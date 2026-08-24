from __future__ import annotations

import datetime as dt
from pathlib import Path

from clickhouse.check_client_telemetry_freshness import CANARY_COUNT_GATE_FROM, evaluate

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 8, 17, 6, 45, tzinfo=dt.UTC)


def _section(**updates: object) -> dict[str, object]:
    section: dict[str, object] = {
        "available": True,
        "state": "calibrating",
        "slo_id": "client_observed",
        "generated_at": "2026-08-17T06:44:30.000Z",
        "canary": {"last_seen_age_seconds": 45, "last_24h_count": 1400},
    }
    section.update(updates)
    return section


def _section_at(section: dict[str, object], now: dt.datetime) -> dict[str, object]:
    """Same section, generated 30s before `now` (keeps the age gate satisfied)."""
    stamped = dict(section)
    stamped["generated_at"] = (
        (now - dt.timedelta(seconds=30)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    return stamped


def _evaluate(payload: dict[str, object], *, now: dt.datetime = NOW) -> list[str]:
    return evaluate(
        payload,
        now=now,
        max_age_seconds=3_600,
        max_canary_age_seconds=3_600,
        min_canary_24h=200,
    )


def test_fresh_calibrating_pipeline_has_no_problems() -> None:
    assert _evaluate({"client_observed": _section()}) == []


def test_missing_section_and_unavailable_states_are_problems() -> None:
    assert _evaluate({"router_core": {}}) == ["/status.json has no client_observed section"]
    assert _evaluate({"client_observed": {"available": False, "reason": "no_data"}}) == [
        "client_observed unavailable: reason='no_data'"
    ]
    assert _evaluate({"client_observed": {"available": False, "reason": "stale"}}) == [
        "client_observed unavailable: reason='stale'"
    ]


def test_stale_snapshot_and_missing_or_old_canary_are_reported() -> None:
    old = _section(generated_at="2026-08-17T04:00:00.000Z")
    assert _evaluate({"client_observed": old}) == [
        "client_reliability snapshot is 9900s old (> 3600s)"
    ]
    never = _section(canary={"last_seen_age_seconds": None, "last_24h_count": 0})
    # Before the ramp-up gate only liveness is enforced; after it, both.
    assert _evaluate({"client_observed": never}) == [
        "canary never seen (last_seen_age_seconds is null)",
    ]
    after = dt.datetime(2026, 8, 19, 8, 0, tzinfo=dt.UTC)
    assert _evaluate({"client_observed": _section_at(never, after)}, now=after) == [
        "canary never seen (last_seen_age_seconds is null)",
        "only 0 canary batches in the last 24h (< 200)",
    ]
    late = _section(canary={"last_seen_age_seconds": 7_200, "last_24h_count": 1400})
    assert _evaluate({"client_observed": late}) == [
        "canary last seen 7200s ago (> 3600s)",
    ]


def test_canary_count_gate_is_deferred_during_ramp_up_but_liveness_is_not() -> None:
    """A brand-new check that cries wolf on its first run teaches people to
    ignore it. The canary started 2026-08-17 04:49 UTC, so a 24 h count cannot
    be met before CANARY_COUNT_GATE_FROM -- but staleness must still fire, or
    the deferral would hide a real outage during the ramp-up."""
    ramping = _section(canary={"last_seen_age_seconds": 45, "last_24h_count": 12})
    before = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)
    after = dt.datetime(2026, 8, 19, 8, 0, tzinfo=dt.UTC)
    assert before < CANARY_COUNT_GATE_FROM <= after

    assert _evaluate({"client_observed": _section_at(ramping, before)}, now=before) == []
    assert _evaluate({"client_observed": _section_at(ramping, after)}, now=after) == [
        "only 12 canary batches in the last 24h (< 200)"
    ]

    # Liveness still fires during the ramp-up.
    dead = _section(canary={"last_seen_age_seconds": 7_200, "last_24h_count": 12})
    assert _evaluate({"client_observed": _section_at(dead, before)}, now=before) == [
        "canary last seen 7200s ago (> 3600s)"
    ]


def test_workflow_reads_the_public_status_page_without_credentials() -> None:
    workflow = (ROOT / ".github/workflows/check-client-telemetry-freshness.yml").read_text()
    assert "clickhouse.check_client_telemetry_freshness" in workflow
    assert "https://trustedrouter.com/status.json" in workflow
    assert "google-github-actions/auth" not in workflow  # public read, no identity
    assert "client-telemetry-freshness" in workflow  # one labelled issue at a time
    assert "schedule:" in workflow


def test_rollout_flips_the_beacon_flag_as_config_as_code() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()
    assert '"TR_CLIENT_EVENTS_ENABLED=true"' in rollout
    # The line above the flag explains how to revert (remove it): the route
    # answers 202 + x-tr-telemetry: off before reading a body.
    assert "x-tr-telemetry: off" in rollout
