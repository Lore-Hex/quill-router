"""Future-dated samples must never disable the staleness detector.

Regression for a live incident: conformance fixtures dated in the year
7748 were written to a production store. The freshness computation took
`max(samples, key=created_at)` and clamped the (negative) age to 0, so
`latest_sample_age_seconds` read 0 and `is_stale` read False permanently —
through what would otherwise be a total monitor outage. A staleness
detector that one poison row disables forever is worse than none.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trusted_router.storage_models import FUTURE_SAMPLE_SKEW_SECONDS, SyntheticProbeSample
from trusted_router.synthetic.rollups import raw_sample_is_within_retention
from trusted_router.synthetic.status import (
    CURRENT_SAMPLE_TTL_SECONDS,
    MONITOR_CADENCE_SECONDS,
    SILENT_PROBE_TTL_SECONDS,
    _current_status,
    _monitor_freshness,
    _slo_current,
)

NOW = dt.datetime(2026, 8, 2, 12, 0, 0, tzinfo=dt.UTC)


def _sample(
    created_at: dt.datetime, *, status: str = "up", sample_id: str = "s1"
) -> SyntheticProbeSample:
    return SyntheticProbeSample(
        id=sample_id,
        probe_type="tls_health",
        target="canonical",
        target_url="https://api-aws.trustedrouter.com/health",
        monitor_region="eu-west-3",
        status=status,
        created_at=created_at.isoformat().replace("+00:00", "Z"),
    )


class TestMonitorFreshness:
    def test_poison_row_does_not_define_latest(self) -> None:
        poison = _sample(dt.datetime(7748, 6, 5, tzinfo=dt.UTC), sample_id="poison")
        real = _sample(NOW - dt.timedelta(seconds=42), sample_id="real")
        freshness = _monitor_freshness([poison, real], now=NOW)
        assert freshness["latest_sample_age_seconds"] == 42
        assert freshness["is_stale"] is False
        assert freshness["future_dated_samples"] == 1

    def test_only_poison_rows_is_stale_not_fresh(self) -> None:
        poison = _sample(dt.datetime(7748, 6, 5, tzinfo=dt.UTC))
        freshness = _monitor_freshness([poison], now=NOW)
        assert freshness["is_stale"] is True
        assert freshness["latest_sample_at"] is None
        assert freshness["future_dated_samples"] == 1

    def test_stale_real_samples_behind_poison_read_stale(self) -> None:
        """The exact live failure shape: monitor died, poison remained."""
        poison = _sample(dt.datetime(7748, 6, 5, tzinfo=dt.UTC), sample_id="poison")
        old = _sample(NOW - dt.timedelta(hours=3), sample_id="old")
        freshness = _monitor_freshness([poison, old], now=NOW)
        assert freshness["is_stale"] is True

    def test_ordinary_clock_skew_tolerated(self) -> None:
        skewed = _sample(NOW + dt.timedelta(seconds=FUTURE_SAMPLE_SKEW_SECONDS - 5))
        freshness = _monitor_freshness([skewed], now=NOW)
        assert freshness["is_stale"] is False
        assert freshness["future_dated_samples"] == 0
        # Display clamps to zero; the decision logic does not.
        assert freshness["latest_sample_age_seconds"] == 0

    def test_empty_is_stale(self) -> None:
        assert _monitor_freshness([], now=NOW)["is_stale"] is True


class TestCurrentStatusPoison:
    def test_future_dated_up_sample_cannot_pin_green(self) -> None:
        poison = _sample(dt.datetime(7748, 6, 5, tzinfo=dt.UTC), status="up")
        current = _current_status([poison], now=NOW)
        (row,) = current["checks"]
        assert row["effective_status"] == "unknown"
        assert current["overall_status"] == "unknown"

    def test_fresh_sample_still_reports_normally(self) -> None:
        fresh = _sample(NOW - dt.timedelta(seconds=30), status="up")
        current = _current_status([fresh], now=NOW)
        assert current["overall_status"] == "up"

    def test_old_sample_still_degrades_to_unknown(self) -> None:
        old = _sample(NOW - dt.timedelta(seconds=CURRENT_SAMPLE_TTL_SECONDS + 60))
        current = _current_status([old], now=NOW)
        assert current["overall_status"] == "unknown"


class TestRetentionUpperBound:
    def test_future_sample_outside_skew_excluded(self) -> None:
        poison = _sample(NOW + dt.timedelta(hours=1))
        assert raw_sample_is_within_retention(poison, now=NOW) is False

    def test_future_sample_inside_skew_included(self) -> None:
        skewed = _sample(NOW + dt.timedelta(seconds=FUTURE_SAMPLE_SKEW_SECONDS - 5))
        assert raw_sample_is_within_retention(skewed, now=NOW) is True

    def test_recent_past_sample_included(self) -> None:
        assert raw_sample_is_within_retention(_sample(NOW - dt.timedelta(days=1)), now=NOW) is True


class TestIngestBoundary:
    @pytest.fixture
    def client(self) -> TestClient:
        from trusted_router.config import Settings
        from trusted_router.main import create_app

        settings = Settings(
            environment="test",
            sentry_dsn=None,
            internal_gateway_token="test-internal-secret",  # noqa: S106 - test fixture.
        )
        return TestClient(create_app(settings, init_observability=False))

    def _post(self, client: TestClient, created_at: str) -> Any:
        return client.post(
            "/v1/internal/synthetic/samples",
            json={
                "samples": [
                    {
                        "id": "ingest-guard-1",
                        "probe_type": "tls_health",
                        "target": "canonical",
                        "target_url": "https://example.com",
                        "monitor_region": "eu-west-3",
                        "status": "up",
                        "created_at": created_at,
                    }
                ]
            },
            headers={"x-trustedrouter-internal-token": "test-internal-secret"},
        )

    def test_far_future_created_at_rejected(self, client: TestClient) -> None:
        response = self._post(client, "7748-06-05T20:10:00Z")
        assert response.status_code == 400
        assert "future" in response.json()["error"]["message"]

    def test_recent_created_at_accepted(self, client: TestClient) -> None:
        recent = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
        assert self._post(client, recent).status_code == 200

    def test_garbage_created_at_rejected_as_400(self, client: TestClient) -> None:
        assert self._post(client, "not-a-timestamp").status_code == 400


class TestSilentProbeDisappearance:
    """A probe that STOPS EMITTING must turn something red.

    The negative control that validated this deployment broke
    reachability, which emits `down` samples. A probe that simply stops
    producing samples emits NOTHING — and nothing was being treated as
    no-opinion ("unknown"), which _worse_status ignores. So the outage
    shape most likely to go unnoticed was the one the page could not
    show. Nothing is not evidence of health.
    """

    def _pair(self, fresh_age: int, stale_age: int) -> list[SyntheticProbeSample]:
        return [
            _sample(NOW - dt.timedelta(seconds=fresh_age), sample_id="fresh"),
            SyntheticProbeSample(
                id="stopped",
                probe_type="attestation_nonce",
                target="canonical",
                target_url="https://api-aws.trustedrouter.com/attestation",
                monitor_region="eu-west-3",
                status="up",
                created_at=(NOW - dt.timedelta(seconds=stale_age))
                .isoformat()
                .replace("+00:00", "Z"),
            ),
        ]

    def test_stopped_probe_is_down_while_siblings_report(self) -> None:
        samples = self._pair(fresh_age=30, stale_age=CURRENT_SAMPLE_TTL_SECONDS + 600)
        current = _current_status(samples, now=NOW)
        by_probe = {c["probe_type"]: c["effective_status"] for c in current["checks"]}
        assert by_probe["attestation_nonce"] == "down"
        assert by_probe["tls_health"] == "up"
        assert current["overall_status"] == "down"

    def test_one_late_monitor_cycle_is_degraded_not_down(self) -> None:
        """Five-minute workers can finish a few seconds late.

        A normal 5m07 cadence must not trigger the deploy watchdog. It remains
        visible as degraded until a second cycle is missed.
        """
        samples = self._pair(
            fresh_age=30,
            stale_age=CURRENT_SAMPLE_TTL_SECONDS + 7,
        )
        current = _current_status(samples, now=NOW)
        by_probe = {c["probe_type"]: c["effective_status"] for c in current["checks"]}
        assert by_probe["attestation_nonce"] == "degraded"
        assert by_probe["tls_health"] == "up"
        assert current["overall_status"] == "degraded"

    def test_control_plane_region_waits_for_two_missed_cycles(self) -> None:
        fresh = SyntheticProbeSample(
            id="eu-fresh",
            probe_type="control_plane_health",
            target="us-central1",
            target_url="https://trusted-router.example/health",
            monitor_region="europe-west4",
            target_region="us-central1",
            status="up",
            created_at=(NOW - dt.timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
        )
        jittered = SyntheticProbeSample(
            id="us-jittered",
            probe_type="control_plane_health",
            target="us-central1",
            target_url="https://trusted-router.example/health",
            monitor_region="us-central1",
            target_region="us-central1",
            status="up",
            created_at=(NOW - dt.timedelta(seconds=CURRENT_SAMPLE_TTL_SECONDS + 7))
            .isoformat()
            .replace("+00:00", "Z"),
        )

        current = _slo_current([fresh, jittered], now=NOW)

        assert current["by_region"]["us-central1"]["status"] == "degraded"
        assert current["by_region"]["us-central1"]["status"] != "down"

    def test_silent_probe_threshold_exceeds_degraded_by_a_full_cycle(self) -> None:
        # The contract is denominated in monitor CADENCES (status.py sizes the
        # degraded boundary at two cadences + startup allowance). "Down" must
        # stay a stronger claim than "degraded" by at least one further full
        # cycle, and must itself cover 3+ cycles of silence -- the
        # silent-disappearance outage, not one late burst.
        assert SILENT_PROBE_TTL_SECONDS >= CURRENT_SAMPLE_TTL_SECONDS + MONITOR_CADENCE_SECONDS
        assert SILENT_PROBE_TTL_SECONDS >= 3 * MONITOR_CADENCE_SECONDS + 60

    def test_whole_monitor_stale_stays_unknown_not_false_outage(self) -> None:
        """Cold start / monitor down: every probe stale. monitor_freshness
        already reports that; marking each probe `down` too would paint a
        false outage on every deploy."""
        samples = self._pair(
            fresh_age=CURRENT_SAMPLE_TTL_SECONDS + 900,
            stale_age=CURRENT_SAMPLE_TTL_SECONDS + 600,
        )
        current = _current_status(samples, now=NOW)
        assert {c["effective_status"] for c in current["checks"]} == {"unknown"}
        assert current["overall_status"] == "unknown"
        assert _monitor_freshness(samples, now=NOW)["is_stale"] is True

    def test_all_fresh_is_untouched(self) -> None:
        samples = self._pair(fresh_age=20, stale_age=40)
        current = _current_status(samples, now=NOW)
        assert {c["effective_status"] for c in current["checks"]} == {"up"}
        assert current["overall_status"] == "up"

    def test_future_dated_still_unknown_never_down(self) -> None:
        """Poison must not be reclassified as an outage — it is absence of
        evidence, and it would page someone for a clock bug."""
        poison = SyntheticProbeSample(
            id="poison",
            probe_type="attestation_nonce",  # distinct key so dedup keeps both
            target="canonical",
            target_url="https://api-aws.trustedrouter.com/attestation",
            monitor_region="eu-west-3",
            status="up",
            created_at="7748-06-05T20:10:00Z",
        )
        samples = [_sample(NOW - dt.timedelta(seconds=30), sample_id="fresh"), poison]
        current = _current_status(samples, now=NOW)
        statuses = {c["probe_type"]: c["effective_status"] for c in current["checks"]}
        assert statuses["attestation_nonce"] == "unknown"
        assert statuses["tls_health"] == "up"

    def test_monitor_reporting_helper(self) -> None:
        from trusted_router.synthetic.status import _monitor_is_reporting

        fresh = [_sample(NOW - dt.timedelta(seconds=10))]
        stale = [_sample(NOW - dt.timedelta(seconds=CURRENT_SAMPLE_TTL_SECONDS + 60))]
        poison = [_sample(dt.datetime(7748, 6, 5, tzinfo=dt.UTC))]
        assert _monitor_is_reporting(fresh, now=NOW) is True
        assert _monitor_is_reporting(stale, now=NOW) is False
        # Poison must not count as "the monitor is alive".
        assert _monitor_is_reporting(poison, now=NOW) is False


def _keyed_sample(
    created_at: dt.datetime, *, target: str, status: str = "up"
) -> SyntheticProbeSample:
    """A sample on its OWN probe key. _slo_current dedupes by
    (monitor_region, target, probe_type), so tests that need two independent
    keys must vary one of those -- not just the id."""
    return SyntheticProbeSample(
        id=f"s-{target}",
        probe_type="tls_health",
        target=target,
        target_url="https://api.trustedrouter.com/health",
        monitor_region="us-central1",
        status=status,
        created_at=created_at.isoformat().replace("+00:00", "Z"),
    )


class TestStalenessIsNotAnOutage:
    """A late probe key must not paint an outage across a healthy fleet.

    Measured 2026-08-26 (n=2374 gaps, 6h of production): p50 180s, p99 400s,
    max 721s. Any freshness contract tighter than that tail will be crossed by
    a normally-behaving monitor, so the contract cannot be the thing that
    decides whether the fleet is up.
    """

    def test_one_late_key_does_not_degrade_the_slo(self) -> None:
        fresh = _keyed_sample(NOW - dt.timedelta(seconds=30), target="canonical")
        late = _keyed_sample(
            NOW - dt.timedelta(seconds=CURRENT_SAMPLE_TTL_SECONDS + 60), target="us-east4"
        )
        current = _slo_current([fresh, late], now=NOW)
        assert current["status"] == "up", (
            "a late-but-not-silent probe is absence of evidence; the SLO must "
            "reflect its last known observation"
        )

    def test_a_silent_key_still_degrades_the_slo(self) -> None:
        fresh = _keyed_sample(NOW - dt.timedelta(seconds=30), target="canonical")
        silent = _keyed_sample(
            NOW - dt.timedelta(seconds=SILENT_PROBE_TTL_SECONDS + 60), target="us-east4"
        )
        current = _slo_current([fresh, silent], now=NOW)
        assert current["status"] != "up", (
            "a probe that stopped while its siblings kept reporting IS evidence"
        )

    def test_silent_boundary_clears_the_measured_cadence_tail(self) -> None:
        # max observed gap 721s; the down boundary must sit above it.
        assert SILENT_PROBE_TTL_SECONDS > 721

    def test_a_late_sample_that_recorded_failure_still_counts(self) -> None:
        # Using the last KNOWN observation must not launder a real failure.
        fresh = _keyed_sample(NOW - dt.timedelta(seconds=30), target="canonical")
        late_bad = _keyed_sample(
            NOW - dt.timedelta(seconds=CURRENT_SAMPLE_TTL_SECONDS + 60),
            target="us-east4",
            status="down",
        )
        current = _slo_current([fresh, late_bad], now=NOW)
        assert current["status"] != "up"
