"""Abuse tripwires: shared client sessions and first-week single-model spend.

Synthetic fixtures only. The ring shape reproduced here: freshly funded
workspaces, almost all spend on one model plus a tiny probe of a second, and
one client session id carried across the accounts.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from trusted_router.config import Settings
from trusted_router.services import abuse_tripwires as tw
from trusted_router.storage import STORE
from trusted_router.storage_models import iso_now

NOW = dt.datetime(2026, 11, 25, 12, 0, tzinfo=dt.UTC)
USD = 1_000_000


class FakeReader:
    def __init__(self, sessions: list[dict[str, Any]] | None = None, spend: list[dict[str, Any]] | None = None) -> None:
        self.sessions = sessions or []
        self.spend = spend or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def shared_client_sessions(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("sessions", kwargs))
        return self.sessions

    def concentrated_model_spend(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("spend", kwargs))
        return self.spend


def _iso(when: dt.datetime) -> str:
    return when.isoformat()


def _workspace(
    email: str,
    *,
    created: dt.datetime = NOW - dt.timedelta(days=2),
    first_topup: dt.datetime | None = None,
    github_created: dt.datetime | None = None,
    payer_matches: bool | None = None,
    paused: bool = False,
) -> str:
    result = STORE.signup(email=email)
    assert result is not None
    workspace = STORE.get_workspace(result.workspace.id)
    user = STORE.get_user(result.user.id)
    assert workspace is not None and user is not None
    workspace.created_at = _iso(created)
    user.created_at = _iso(created)
    if github_created is not None:
        STORE.set_user_github_identity(user.id, "someone", _iso(github_created))
    if payer_matches is not None:
        STORE.set_user_paypal_payer_match(user.id, payer_matches)
    if paused:
        workspace.billing_paused = True
    if first_topup is not None:
        from trusted_router.storage_models import AcquisitionAttribution

        STORE.create_acquisition_attribution(
            AcquisitionAttribution(
                workspace_id=workspace.id, anonymous_id="anon", first_touch={}, last_touch={},
                signup_provider="github",
            )
        )
        STORE.record_acquisition_purchase(workspace.id, amount_microdollars=25 * USD, occurred_at=_iso(first_topup))
    return workspace.id


def _spend(workspace_id: str, *, total: int, top: int, models: int) -> dict[str, Any]:
    return {
        "workspace_id": workspace_id, "total_microdollars": total, "top_model_microdollars": top,
        "models": models, "top_model": "vendor/one-model", "app": "Shared Client",
    }


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str], dict[str, str]]]:
    alerts: list[tuple[str, list[str], dict[str, str]]] = []

    def fake_ops_alert(message: str, *, fingerprint: list[str], tags: dict[str, str] | None = None, **_: Any) -> bool:
        alerts.append((message, fingerprint, tags or {}))
        return True

    monkeypatch.setattr(tw, "ops_alert", fake_ops_alert)
    return alerts


def test_ring_shape_fires_both_tripwires_with_signals_and_workspace_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts = _capture(monkeypatch)
    first = _workspace(
        "a@example.com", first_topup=NOW - dt.timedelta(days=1),
        github_created=NOW - dt.timedelta(days=2, seconds=20), payer_matches=False,
    )
    second = _workspace("b@example.com", first_topup=NOW - dt.timedelta(hours=5))
    reader = FakeReader(
        sessions=[{"session_id": "ses_synthetic1", "workspace_ids": [first, second], "app": "Shared Client"}],
        # 99.9% on one model plus a tiny probe of a second: models_used=1 misses it.
        spend=[_spend(first, total=40 * USD, top=39_960_000, models=2)],
    )

    emitted = tw.run_tripwire_sweep(reader, STORE, now=NOW)

    assert len(emitted) == 2
    session_alert, concentration_alert = (a[0] for a in alerts)
    assert "shared_session" in session_alert
    assert first in session_alert and second in session_alert
    assert "ses_synthetic1" not in session_alert  # digest only
    assert f"{first}[single_model_concentration,new_github_account,paypal_payer_mismatch]" in session_alert
    assert f"workspace={first}" in concentration_alert
    assert "signals=single_model_concentration,shared_session,new_github_account,paypal_payer_mismatch" in concentration_alert
    assert "models_used_1" not in concentration_alert
    assert alerts[1][2]["workspace_id"] == first
    # Query windows and thresholds are the module's.
    assert reader.calls[1][1]["min_share_basis_points"] == 9_500
    assert reader.calls[1][1]["min_total_microdollars"] == 5 * USD
    assert reader.calls[0][1]["min_workspaces"] == 2


def test_findings_alert_once_and_session_realerts_when_a_workspace_joins(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts = _capture(monkeypatch)
    a = _workspace("a@example.com")
    b = _workspace("b@example.com")
    c = _workspace("c@example.com")
    reader = FakeReader(sessions=[{"session_id": "ses_x", "workspace_ids": [a, b]}])

    assert len(tw.run_tripwire_sweep(reader, STORE, now=NOW)) == 1
    assert tw.run_tripwire_sweep(reader, STORE, now=NOW) == []
    reader.sessions = [{"session_id": "ses_x", "workspace_ids": [a, b, c]}]
    assert len(tw.run_tripwire_sweep(reader, STORE, now=NOW)) == 1
    assert len(alerts) == 2


def test_models_used_one_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts = _capture(monkeypatch)
    workspace = _workspace("a@example.com", first_topup=NOW - dt.timedelta(days=3))

    tw.run_tripwire_sweep(FakeReader(spend=[_spend(workspace, total=10 * USD, top=10 * USD, models=1)]), STORE, now=NOW)

    assert "signals=single_model_concentration,models_used_1" in alerts[0][0]


@pytest.mark.parametrize(("total", "top", "first_topup_days", "fires"), [
    (5 * USD, 4_750_000, 6.9, True),       # exactly 95 %, exactly the floor, inside the window
    (5 * USD, 4_749_999, 1, False),        # just under 95 %
    (4_999_999, 4_999_999, 1, False),      # just under the spend floor
    (50 * USD, 50 * USD, 7.1, False),      # established: first top-up outside the window
])
def test_concentration_thresholds(
    monkeypatch: pytest.MonkeyPatch, total: int, top: int, first_topup_days: float, fires: bool,
) -> None:
    alerts = _capture(monkeypatch)
    workspace = _workspace(
        "a@example.com",
        created=NOW - dt.timedelta(days=400),
        first_topup=NOW - dt.timedelta(days=first_topup_days),
    )

    tw.run_tripwire_sweep(FakeReader(spend=[_spend(workspace, total=total, top=top, models=3)]), STORE, now=NOW)

    assert bool(alerts) is fires


def test_old_workspace_without_purchase_record_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts = _capture(monkeypatch)
    workspace = _workspace("a@example.com", created=NOW - dt.timedelta(days=90))

    tw.run_tripwire_sweep(FakeReader(spend=[_spend(workspace, total=90 * USD, top=90 * USD, models=1)]), STORE, now=NOW)

    assert alerts == []


def test_already_paused_workspaces_do_not_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts = _capture(monkeypatch)
    a = _workspace("a@example.com", paused=True, first_topup=NOW - dt.timedelta(days=1))
    b = _workspace("b@example.com", paused=True)
    reader = FakeReader(
        sessions=[{"session_id": "ses_y", "workspace_ids": [a, b]}],
        spend=[_spend(a, total=40 * USD, top=40 * USD, models=1)],
    )

    assert tw.run_tripwire_sweep(reader, STORE, now=NOW) == []
    assert alerts == []


def test_payer_mismatch_only_reported_for_first_week_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts = _capture(monkeypatch)
    old = _workspace("old@example.com", created=NOW - dt.timedelta(days=30), payer_matches=False)
    new = _workspace("new@example.com", created=NOW - dt.timedelta(days=1), payer_matches=False)

    tw.run_tripwire_sweep(FakeReader(sessions=[{"session_id": "s", "workspace_ids": [old, new]}]), STORE, now=NOW)

    assert f"{old}[-]" in alerts[0][0]
    assert f"{new}[paypal_payer_mismatch]" in alerts[0][0]


def test_sweep_cap_defers_rest_to_next_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts = _capture(monkeypatch)
    monkeypatch.setattr(tw, "MAX_ALERTS_PER_SWEEP", 2)
    workspaces = [_workspace(f"w{i}@example.com") for i in range(4)]
    sessions = [
        {"session_id": f"s{i}", "workspace_ids": [workspaces[i], workspaces[(i + 1) % 4]]} for i in range(3)
    ]

    assert len(tw.run_tripwire_sweep(FakeReader(sessions=sessions), STORE, now=NOW)) == 2
    assert any("deferred=1" in message for message, _, _ in alerts)
    assert len(tw.run_tripwire_sweep(FakeReader(sessions=sessions), STORE, now=NOW)) == 1


def test_hourly_entry_runs_once_per_hour_and_respects_switches() -> None:
    reader = FakeReader()
    enabled = Settings(environment="test")  # type: ignore[call-arg]

    assert tw.run_hourly_tripwire_sweep(enabled, STORE, reader=reader, now=NOW) == []
    assert tw.run_hourly_tripwire_sweep(enabled, STORE, reader=reader, now=NOW + dt.timedelta(minutes=30)) is None
    assert tw.run_hourly_tripwire_sweep(enabled, STORE, reader=reader, now=NOW + dt.timedelta(hours=1)) == []
    disabled = Settings(environment="test", abuse_tripwires_enabled=False)  # type: ignore[call-arg]
    assert tw.run_hourly_tripwire_sweep(disabled, STORE, reader=reader, now=NOW + dt.timedelta(hours=2)) is None
    # No ClickHouse reader configured: a no-op, not an error.
    assert tw.run_hourly_tripwire_sweep(enabled, STORE, now=NOW + dt.timedelta(hours=3)) is None


@pytest.mark.parametrize(("github_created", "signed_up", "new"), [
    ("2026-11-24T12:00:20Z", "2026-11-24T12:00:40Z", True),
    ("2026-11-23T12:00:41Z", "2026-11-24T12:00:40Z", True),
    ("2026-11-23T12:00:40Z", "2026-11-24T12:00:40Z", False),
    (None, "2026-11-24T12:00:40Z", False),
    ("garbage", "2026-11-24T12:00:40Z", False),
])
def test_github_account_was_new(github_created: str | None, signed_up: str, new: bool) -> None:
    assert tw.github_account_was_new(github_created, signed_up) is new


def test_new_github_signup_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts = _capture(monkeypatch)

    assert tw.alert_new_github_signup(workspace_id="ws-1", github_created_at=_iso(NOW - dt.timedelta(seconds=20)), now=NOW)
    assert not tw.alert_new_github_signup(workspace_id="ws-2", github_created_at=_iso(NOW - dt.timedelta(days=3)), now=NOW)
    assert not tw.alert_new_github_signup(workspace_id="ws-3", github_created_at=None, now=NOW)

    assert len(alerts) == 1
    assert "workspace=ws-1" in alerts[0][0] and "signals=new_github_account" in alerts[0][0]


def test_parse_time_accepts_clickhouse_and_iso_forms() -> None:
    assert tw.parse_time("2026-11-25 12:00:00.000") == NOW
    assert tw.parse_time("2026-11-25T12:00:00Z") == NOW
    assert tw.parse_time(iso_now()) is not None
    assert tw.parse_time("") is None


def test_clickhouse_reads_bind_every_value_as_a_parameter() -> None:
    import httpx

    from trusted_router.operational_analytics import OperationalAnalyticsClient

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"workspace_id": "ws"}]})

    client = OperationalAnalyticsClient(
        base_url="http://clickhouse.invalid:8123", user="reader", password="pw",  # noqa: S106
        transport=httpx.MockTransport(handler),
    )
    start = "2026-11-18T12:00:00+00:00"

    assert client.shared_client_sessions(start_at=start, min_workspaces=1, limit=10) == [{"workspace_id": "ws"}]
    client.concentrated_model_spend(start_at=start, min_total_microdollars=5, min_share_basis_points=20_000, limit=10)

    sessions, spend = (dict(request.url.params) for request in seen)
    assert sessions["param_start_at"] == start
    assert sessions["param_min_workspaces"] == "2"  # never below two workspaces
    assert spend["param_min_share_bp"] == "10000"  # clamped to 100 %
    assert spend["param_min_total"] == "5"
    for request in seen:
        body = request.content.decode()
        assert start not in body
        assert "FROM activity_generations" in body
    assert "usage_type = 'Credits'" in seen[1].content.decode()
