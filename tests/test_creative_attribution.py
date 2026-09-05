from __future__ import annotations

import datetime as dt
import json
import logging
import queue
import runpy
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import trusted_router.marketing_funnel as funnel
from trusted_router.acquisition import record_successful_api_call
from trusted_router.axiom_config import _DroppingQueueHandler
from trusted_router.main import _ApplicationConsoleFormatter


def test_creative_survives_real_signup_activation_payment_and_axiom_queue(
    client: TestClient, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    assert client.get(
        "/openrouter-alternative?utm_source=google&utm_medium=cpc"
        "&utm_campaign=creative_regression&utm_content=privacy_a&gclid=test-click"
    ).status_code == 200
    assert client.post("/analytics/events", json={"event": "landing_engaged"}).status_code == 204
    signup = client.post("/v1/signup", json={"email": "creative@example.com"})
    assert signup.status_code == 201
    workspace_id = signup.json()["data"]["workspace_id"]
    assert client.get("/?utm_source=other&utm_content=later_visit").status_code == 200
    record_successful_api_call(workspace_id, model="test/model", provider="test")
    response = client.post("/v1/internal/stripe/webhook", json={
        "id": "evt_creative_regression",
        "type": "checkout.session.completed",
        "data": {"object": {
            "amount_total": 500, "payment_intent": "pi_creative_regression",
            "payment_status": "paid", "metadata": {"workspace_id": workspace_id},
        }},
    })
    assert response.status_code == 200
    stages = {
        "acquisition.landing_engaged", "acquisition.signup_completed",
        "acquisition.first_successful_api_call", "acquisition.credit_purchase_completed",
    }
    handler = _DroppingQueueHandler(queue.Queue())
    seen = set()
    for record in caplog.records:
        if record.getMessage() not in stages:
            continue
        record.prompt = "private-prompt-canary"
        record.output = "private-output-canary"
        prepared = handler.prepare(record)
        assert getattr(prepared, "creative_id", None) == "privacy_a"
        assert prepared.utm_content == "[Filtered]"
        assert prepared.prompt == prepared.output == "[Filtered]"
        cloud_record = json.loads(_ApplicationConsoleFormatter().format(record))
        assert cloud_record["creative_id"] == "privacy_a"
        assert "prompt" not in cloud_record and "output" not in cloud_record
        assert "private-prompt-canary" not in json.dumps(prepared.__dict__)
        assert "private-output-canary" not in json.dumps(prepared.__dict__)
        seen.add(record.getMessage())
    assert seen == stages


def test_creative_alias_still_scrubs_secret_values() -> None:
    handler = _DroppingQueueHandler(queue.Queue())
    record = logging.makeLogRecord({
        "msg": "acquisition.signup_completed", "levelno": logging.INFO,
        "creative_id": "sk-tr-v1-private-secret-canary",
        "first_creative_id": "ghp_private-secret-canary",
    })
    prepared = handler.prepare(record)
    assert prepared.creative_id == "[Filtered]"
    assert prepared.first_creative_id == "[Filtered]"


def _event(event: str, creative: str = "[Filtered]") -> dict[str, object]:
    return {
        "event": event, "anonymous_fingerprint": "a" * 64,
        "first_at": "2026-09-01T12:00:00Z", "events": 1,
        "utm_source": "creator", "utm_medium": "sponsorship",
        "utm_campaign": "creator_pilot", "utm_content": creative,
        "landing_path": "/for-developers", "revenue_microdollars": 5_000_000,
    }


@pytest.mark.parametrize("query", [funnel.build_axiom_funnel_query, funnel.build_axiom_cohort_query])
def test_query_normalizes_creative_before_aggregation(query) -> None:
    apl = query("test-logs")
    assert "column_ifexists('creative_id', '')" in apl
    assert apl.index("creative_id") < apl.index("summarize")


@pytest.mark.parametrize("old", [None, "", "[Filtered]", "legacy"])
def test_summary_prefers_new_creative_identifier(old: str | None) -> None:
    record = _event("acquisition.signup_completed")
    record.update(utm_content=old, creative_id="new_creative", people=2)
    rows = funnel.aggregate_funnel_rows([record], creative="new_creative")
    assert len(rows) == 1
    assert rows[0].signups == 2


def test_recovery_uses_matching_evidence_without_duplicating_payments() -> None:
    record = _event("acquisition.credit_purchase_completed")
    evidence = _event("acquisition.credit_purchase_completed", "mehul_demo")
    fixed, counts = funnel.recover_creative_attribution([record], [evidence, evidence])
    assert len(fixed) == 1
    assert fixed[0]["creative_id"] == "mehul_demo"
    assert fixed[0]["events"] == 1
    assert fixed[0]["revenue_microdollars"] == 5_000_000
    assert "creative_id" not in record
    assert counts["recovered_records"] == 1
    assert counts["unresolved_records"] == 0


@pytest.mark.parametrize("change", [
    {"anonymous_fingerprint": "b" * 64}, {"utm_campaign": "another_campaign"},
    {"event": "acquisition.signup_completed"}, {"landing_path": "/other"},
    {"experiment_cell_id": "another_cell"},
])
def test_recovery_never_guesses_across_visitors_or_touches(change) -> None:
    record = _event("acquisition.credit_purchase_completed")
    evidence = _event("acquisition.credit_purchase_completed", "mehul_demo")
    evidence.update(change)
    fixed, counts = funnel.recover_creative_attribution([record], [evidence])
    assert "creative_id" not in fixed[0]
    assert counts["unresolved_records"] == 1


def test_recovery_requires_unambiguous_evidence_and_keeps_existing_id() -> None:
    record = _event("acquisition.signup_completed")
    evidence = [_event("acquisition.signup_completed", name) for name in ("a", "b")]
    fixed, counts = funnel.recover_creative_attribution([record], evidence)
    assert "creative_id" not in fixed[0]
    assert counts["unresolved_records"] == 1
    record["creative_id"] = "original"
    fixed, counts = funnel.recover_creative_attribution([record], evidence)
    assert fixed[0]["creative_id"] == "original"
    assert counts["recovered_records"] == 0


@pytest.mark.parametrize("change", [
    {"events": 2}, {"first_at": "2026-09-02T12:00:00Z"},
])
def test_recovery_rejects_incomplete_or_different_time_evidence(change) -> None:
    record = _event("acquisition.credit_purchase_completed")
    record.update(change)
    evidence = _event("acquisition.credit_purchase_completed", "mehul_demo")
    fixed, counts = funnel.recover_creative_attribution([record], [evidence])
    assert "creative_id" not in fixed[0]
    assert counts["unresolved_records"] == 1


def test_capped_axiom_results_are_split_without_counting_partial_parent(monkeypatch) -> None:
    report = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/marketing_funnel_report.py"))
    start = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    midpoint = start + dt.timedelta(hours=12)
    end = start + dt.timedelta(days=1)
    calls = []

    def run(command, **kwargs):
        left = command[command.index("--start-time") + 1]
        right = command[command.index("--end-time") + 1]
        calls.append((left, right))
        assert f"_time >= datetime({left}) and _time < datetime({right})" in command[2]
        assert kwargs["timeout"] == 120
        if (left, right) == (start.isoformat(), end.isoformat()):
            rows = [{"partial": True}] * 1000
        else:
            rows = [_event("acquisition.credit_purchase_completed", "creative_a")]
        return subprocess.CompletedProcess(command, 0, "\n".join(json.dumps(row) for row in rows), "")

    monkeypatch.setattr(subprocess, "run", run)
    records = report["fetch_axiom_cohort_records"]("axiom", "test", start_at=start, end_at=end)
    assert calls == [(start.isoformat(), end.isoformat()),
                     (start.isoformat(), midpoint.isoformat()),
                     (midpoint.isoformat(), end.isoformat())]
    assert len(records) == 2
    assert sum(row["revenue_microdollars"] for row in records) == 10_000_000
    assert all("partial" not in row for row in records)


@pytest.mark.parametrize("exit_code", [0, 1])
def test_axiom_report_fails_closed_on_unreadable_or_irreducible_window(monkeypatch, exit_code) -> None:
    report = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/marketing_funnel_report.py"))
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, exit_code, '{}\n' * 1000, 'failed'))
    start = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    with pytest.raises(SystemExit):
        report["fetch_axiom_cohort_records"](
            "axiom", "test", start_at=start, end_at=start + dt.timedelta(seconds=1),
        )


def test_cloud_evidence_retains_only_safe_metadata_and_cohort_uses_new_id() -> None:
    record = _event("acquisition.landing_engaged")
    record.update(creative_id="mehul_demo", prompt="private", email="person@example.com")
    cloud = funnel.parse_cloud_logging_acquisition_events(json.dumps([
        {"timestamp": "2026-09-01T12:00:00Z", "jsonPayload": record},
    ]))
    assert "prompt" not in cloud[0] and "email" not in cloud[0]
    assert cloud[0]["creative_id"] == "mehul_demo"
    rows = funnel.aggregate_cohort_funnel_rows(
        cloud, cohort_start=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        cohort_end=dt.datetime(2026, 9, 2, tzinfo=dt.UTC),
        observed_through=dt.datetime(2026, 9, 3, tzinfo=dt.UTC),
    )
    assert rows[0].creative == "mehul_demo"


def test_unrecoverable_creative_is_explicit_and_cannot_win_experiment() -> None:
    record = _event("acquisition.signup_completed")
    record["people"] = 3
    row = funnel.aggregate_funnel_rows([record])[0]
    assert row.creative == "(unattributed)"
    row.engaged_visitors = 200
    row.activated_users = 20
    assert funnel.experiment_state(row) == "attribution_incomplete"
