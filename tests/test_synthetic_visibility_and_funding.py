"""Tests for the 2026-08 synthetic-visibility fix.

Three failure modes, one incident: model probes failed 100% on two
deployments while (a) the status page rendered green, (b) nobody was
alerted, and (c) the root cause was a dry monitor workspace that could
only be refilled by hand. Each test class below pins one of the fixes:

- Model Inference component + banner degradation (components.py, status.py)
- Monthly idempotent monitor self-funding on the authorize path (funding.py)
- Sentry alert at exactly the Nth consecutive probe failure (alerts.py)
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from trusted_router.catalog import CHEAP_MODEL_ID, MONITOR_MODEL_ID
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.security import lookup_hash_api_key
from trusted_router.storage import STORE
from trusted_router.storage_models import SyntheticProbeSample, iso_now, utcnow
from trusted_router.synthetic import funding
from trusted_router.synthetic.alerts import alert_on_failure_streak
from trusted_router.synthetic.funding import ensure_monitor_funding
from trusted_router.synthetic.status import status_snapshot


@pytest.fixture(autouse=True)
def _reset_funding_caches() -> None:
    funding.reset_for_tests()


def _sample(
    *,
    id: str,
    probe_type: str,
    status: str,
    target: str = "canonical",
    created_at: str | None = None,
    error_type: str | None = None,
    http_status: int | None = None,
    monitor_region: str = "us-central1",
) -> SyntheticProbeSample:
    return SyntheticProbeSample(
        id=id,
        probe_type=probe_type,
        target=target,
        target_url="https://api.trustedrouter.com/v1",
        monitor_region=monitor_region,
        target_region="us-central1",
        status=status,
        error_type=error_type,
        http_status=http_status,
        created_at=created_at or iso_now(),
    )


def _recent(now: dt.datetime, seconds: int) -> str:
    return (now - dt.timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _monitor_settings() -> Settings:
    # model_inference is published only where the deployment actually runs
    # pongs (COMPONENT_REQUIRED_CAPABILITIES keys off the monitor key).
    return Settings(environment="test", synthetic_monitor_api_key="sk-tr-monitor-status")


# ---------------------------------------------------------------------------
# Model Inference component: pong failures must render, not vanish.
# ---------------------------------------------------------------------------


def _core_up_samples(now: dt.datetime) -> list[SyntheticProbeSample]:
    return [
        _sample(
            id="syn_tls",
            probe_type="tls_health",
            status="up",
            created_at=_recent(now, 10),
        ),
        _sample(
            id="syn_settle",
            target="control-plane",
            probe_type="gateway_authorize_settle",
            status="up",
            created_at=_recent(now, 11),
        ),
        _sample(
            id="syn_fallback",
            target="control-plane",
            probe_type="provider_fallback",
            status="up",
            created_at=_recent(now, 12),
        ),
    ]


def test_failing_pongs_take_down_model_inference_and_the_banner() -> None:
    now = utcnow()
    samples = _core_up_samples(now) + [
        _sample(
            id="syn_pong_402",
            probe_type="openai_sdk_pong",
            status="down",
            error_type="pong_mismatch",
            http_status=402,
            created_at=_recent(now, 13),
        ),
        _sample(
            id="syn_responses_402",
            probe_type="responses_pong",
            status="down",
            error_type="pong_mismatch",
            http_status=402,
            created_at=_recent(now, 14),
        ),
    ]

    snapshot = status_snapshot(samples, now=now, settings=_monitor_settings())

    components = {row["id"]: row for row in snapshot["components"]}
    assert components["model_inference"]["status"] == "down"
    # The banner is honest: degraded, naming the failing surface — while the
    # router_core SLO deliberately does not burn (July scoping decision).
    assert snapshot["overall_status"] == "degraded"
    assert snapshot["summary"]["headline"] == "Partial Outage: Model Inference"
    assert "Model Inference" in snapshot["summary"]["detail"]
    assert snapshot["slo_classes"]["router_core"]["status"] == "up"


def test_passing_pongs_keep_model_inference_and_banner_green() -> None:
    now = utcnow()
    samples = _core_up_samples(now) + [
        _sample(
            id="syn_pong_ok",
            probe_type="openai_sdk_pong",
            status="up",
            created_at=_recent(now, 13),
        ),
        _sample(
            id="syn_responses_ok",
            probe_type="responses_pong",
            status="up",
            created_at=_recent(now, 14),
        ),
    ]

    snapshot = status_snapshot(samples, now=now, settings=_monitor_settings())

    components = {row["id"]: row for row in snapshot["components"]}
    assert components["model_inference"]["status"] == "up"
    assert snapshot["overall_status"] == "up"
    assert snapshot["summary"]["headline"] == "All Systems Operational"


def test_monitor_configuration_errors_stay_out_of_the_component() -> None:
    # A paused/unavailable monitor account is monitor trouble, not a public
    # outage: those samples are excluded from every component, so they must
    # not paint Model Inference red either.
    now = utcnow()
    samples = _core_up_samples(now) + [
        _sample(
            id="syn_pong_monitor_cfg",
            probe_type="openai_sdk_pong",
            status="down",
            error_type="monitor_account_unavailable",
            created_at=_recent(now, 13),
        ),
    ]

    snapshot = status_snapshot(samples, now=now, settings=_monitor_settings())

    components = {row["id"]: row for row in snapshot["components"]}
    assert components["model_inference"]["status"] == "unknown"
    assert snapshot["overall_status"] == "up"


# ---------------------------------------------------------------------------
# Monitor self-funding: monthly, idempotent, applied on the authorize path.
# ---------------------------------------------------------------------------


def test_monitor_authorize_applies_monthly_grant_exactly_once() -> None:
    monitor_key = "sk-tr-monitor-funding-test"  # noqa: S105 - test key.
    app = create_app(
        Settings(environment="test", synthetic_monitor_api_key=monitor_key),
        init_observability=False,
    )
    local_client = TestClient(app)
    monitor_user = STORE.ensure_user("monitor", email="monitor@trustedrouter.local")
    monitor_workspace = STORE.list_workspaces_for_user(monitor_user.id)[0]
    STORE.create_api_key(
        workspace_id=monitor_workspace.id,
        name="Synthetic monitor",
        creator_user_id=monitor_user.id,
        raw_key=monitor_key,
    )
    before = STORE.credit_money_snapshot(monitor_workspace.id)
    assert before is not None

    body = {
        "api_key_lookup_hash": lookup_hash_api_key(monitor_key),
        "model": MONITOR_MODEL_ID,
        "estimated_input_tokens": 1,
        "max_output_tokens": 1,
    }
    first = local_client.post("/v1/internal/gateway/authorize", json=body)
    assert first.status_code == 200, first.text
    after_first = STORE.credit_money_snapshot(monitor_workspace.id)
    assert after_first is not None
    granted = after_first[0] - before[0]
    assert granted == 200_000_000  # $200 in microdollars

    second = local_client.post("/v1/internal/gateway/authorize", json=body)
    assert second.status_code == 200, second.text
    after_second = STORE.credit_money_snapshot(monitor_workspace.id)
    assert after_second is not None
    assert after_second[0] == after_first[0]  # same month: no double grant

    # Process restart within the same month: the in-process marker is gone
    # but the ledger's per-event idempotency still blocks a second grant.
    funding.reset_for_tests()
    third = local_client.post("/v1/internal/gateway/authorize", json=body)
    assert third.status_code == 200, third.text
    after_third = STORE.credit_money_snapshot(monitor_workspace.id)
    assert after_third is not None
    assert after_third[0] == after_first[0]

    # A new month is a new event id, so the grant applies again.
    settings = Settings(environment="test", synthetic_monitor_api_key=monitor_key)
    next_month = dt.datetime(2027, 1, 15, tzinfo=dt.UTC)
    assert ensure_monitor_funding(STORE, settings, monitor_workspace.id, now=next_month)
    after_next_month = STORE.credit_money_snapshot(monitor_workspace.id)
    assert after_next_month is not None
    assert after_next_month[0] == after_first[0] + 200_000_000


def test_non_monitor_authorize_never_grants(client: TestClient, inference_key: str) -> None:
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_lookup_hash": lookup_hash_api_key(inference_key),
            "model": CHEAP_MODEL_ID,
            "estimated_input_tokens": 1,
            "max_output_tokens": 1,
        },
    )
    assert authorize.status_code == 200, authorize.text
    workspace_id = authorize.json()["data"]["workspace_id"]
    snapshot = STORE.credit_money_snapshot(workspace_id)
    assert snapshot is not None
    # Trial credit only — no $200 monitor grant leaked onto a customer key.
    assert snapshot[0] < 200_000_000


def test_zero_grant_setting_disables_funding() -> None:
    calls: list[tuple[str, int, str]] = []

    class _Store:
        def credit_workspace_typed_direct(
            self, workspace_id: str, amount: int, event_id: str
        ) -> bool:
            calls.append((workspace_id, amount, event_id))
            return True

    settings = Settings(
        environment="test",
        synthetic_monitor_monthly_grant_dollars=0.0,
    )
    assert ensure_monitor_funding(_Store(), settings, "ws_x") is False
    assert calls == []


# ---------------------------------------------------------------------------
# Sentry alert: fires at exactly the Nth consecutive failure, once.
# ---------------------------------------------------------------------------


class _StreakStore:
    def __init__(self, statuses_newest_first: list[str]) -> None:
        now = utcnow()
        self.rows = [
            _sample(
                id=f"syn_streak_{index}",
                probe_type="openai_sdk_pong",
                status=status,
                created_at=_recent(now, index),
            )
            for index, status in enumerate(statuses_newest_first)
        ]
        self.queries: list[dict[str, object]] = []

    def synthetic_probe_samples(self, **kwargs: object) -> list[SyntheticProbeSample]:
        self.queries.append(kwargs)
        limit = int(kwargs.get("limit") or 0)
        return self.rows[:limit]


@pytest.fixture
def sentry_events(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import sentry_sdk

    events: list[str] = []
    monkeypatch.setattr(
        sentry_sdk,
        "capture_message",
        lambda message, level=None: events.append(message),
    )
    return events


def test_alert_fires_at_exactly_the_third_consecutive_failure(
    sentry_events: list[str],
) -> None:
    store = _StreakStore(["down", "down", "down", "up"])
    fired = alert_on_failure_streak(store, store.rows[0])
    assert fired is True
    assert len(sentry_events) == 1
    assert "openai_sdk_pong" in sentry_events[0]
    assert store.queries[0]["probe_type"] == "openai_sdk_pong"
    assert store.queries[0]["target"] == "canonical"


def test_alert_stays_silent_below_threshold(sentry_events: list[str]) -> None:
    store = _StreakStore(["down", "down", "up", "down"])
    assert alert_on_failure_streak(store, store.rows[0]) is False
    assert sentry_events == []


def test_alert_fires_in_the_two_sample_transition_window(
    sentry_events: list[str],
) -> None:
    # streak == threshold + 1 still fires: with two concurrent writers (GCP's
    # two monitor regions) the observed streak can jump 2 -> 4, and an exact
    # == threshold match would suppress the alert forever. The Sentry
    # fingerprint folds a double fire at 3 and 4 into one issue.
    store = _StreakStore(["down", "down", "down", "down", "up"])
    assert alert_on_failure_streak(store, store.rows[0]) is True
    assert len(sentry_events) == 1


def test_alert_does_not_refire_past_the_transition_window(
    sentry_events: list[str],
) -> None:
    # 5th consecutive failure: the issue already exists; stay silent until
    # the probe recovers and a fresh streak forms.
    store = _StreakStore(["down", "down", "down", "down", "down"])
    assert alert_on_failure_streak(store, store.rows[0]) is False
    assert sentry_events == []


def test_alert_ignores_up_samples_and_never_queries(sentry_events: list[str]) -> None:
    store = _StreakStore(["up"])
    assert alert_on_failure_streak(store, store.rows[0]) is False
    assert store.queries == []
    assert sentry_events == []


def test_alert_survives_store_failure(sentry_events: list[str]) -> None:
    class _BrokenStore:
        def synthetic_probe_samples(self, **kwargs: object) -> list[SyntheticProbeSample]:
            raise RuntimeError("bigtable unavailable")

    sample = _sample(id="syn_broken", probe_type="openai_sdk_pong", status="down")
    assert alert_on_failure_streak(_BrokenStore(), sample) is False
    assert sentry_events == []


def test_record_probe_samples_invokes_streak_alerting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.routes.internal import synthetic as synthetic_routes

    seen: list[str] = []
    monkeypatch.setattr(
        synthetic_routes,
        "alert_on_failure_streak",
        lambda store, sample: seen.append(sample.id),
    )
    samples = [
        _sample(id="syn_wire_1", probe_type="openai_sdk_pong", status="down"),
        _sample(id="syn_wire_2", probe_type="responses_pong", status="up"),
    ]
    synthetic_routes._record_probe_samples(samples)
    assert seen == ["syn_wire_1", "syn_wire_2"]
