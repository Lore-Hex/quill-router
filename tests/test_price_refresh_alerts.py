import copy
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError

import pytest
import yaml

from scripts import price_refresh_alerts as alerts

NOW = datetime(2026, 10, 29, 23, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


def run(run_id=100, conclusion="failure", *, when=NOW):
    return {
        "id": run_id, "run_attempt": 1, "head_sha": "a" * 40,
        "path": ".github/workflows/refresh-prices.yml", "head_branch": "main",
        "repository": {"full_name": alerts.REPOSITORY}, "event": "schedule",
        "conclusion": conclusion, "updated_at": when.isoformat(),
        "logs_url": "secret-log-body", "display_title": "private-provider-response",
    }


def jobs(*, validated=False, started=True):
    return [{"steps": [{
        "name": alerts.MARKER_STEP if validated else "Commit, verify, and push if changed",
        "number": 14, "conclusion": "success" if validated else "failure",
        "started_at": (NOW - timedelta(minutes=1)).isoformat() if started else None,
        "completed_at": NOW.isoformat(), "log": "sk-tr-v1-must-not-export",
    }]}]


@pytest.fixture
def emitted(monkeypatch):
    events = []
    monkeypatch.setattr(alerts, "send_sentry", lambda kind, payload: events.append((kind, payload)))
    return events


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "action_required", "startup_failure", "cancelled"])
def test_active_failures_have_bounded_safe_context(conclusion):
    details = alerts.failed_run(run(conclusion=conclusion), jobs())
    assert details == {
        "run_id": 100, "attempt": 1, "commit": "a" * 40,
        "run_url": "https://github.com/Lore-Hex/quill-router/actions/runs/100",
        "conclusion": conclusion, "stage": "publication", "step_number": 14,
    }


def test_superseded_queued_cancellation_is_not_a_failure():
    assert alerts.failed_run(run(conclusion="cancelled"), jobs(started=False)) is None
    assert alerts.failed_run(run(conclusion="success"), jobs()) is None
    assert alerts.failed_run(run(), [])['stage'] == 'runner'


def test_success_requires_actual_publication_marker():
    assert alerts.validated_at(jobs()) is None
    assert alerts.validated_at(jobs(validated=True)) == NOW.isoformat()


@pytest.mark.parametrize("key,value", [
    ("head_branch", "untrusted"), ("event", "pull_request"),
    ("head_sha", "private prompt"), ("path", ".github/workflows/evil.yml"),
    ("repository", {"full_name": "other/repo"}),
])
def test_rejects_untrusted_trigger_metadata(key, value):
    candidate = run()
    candidate[key] = value
    with pytest.raises(ValueError):
        alerts.run_metadata(candidate)


def test_cooldown_survives_new_runs_and_reminders_are_bounded(emitted):
    state = {"sent": {}}
    assert alerts.report("refresh_failed", alerts.failed_run(run(), jobs()), state, NOW)
    # Simulate a new process loading the durable ledger.
    state = json.loads(json.dumps(state))
    assert not alerts.report("refresh_failed", {}, state, NOW + timedelta(hours=5, minutes=59))
    assert alerts.report("refresh_failed", {}, state, NOW + timedelta(hours=6))
    assert len(emitted) == 2
    assert emitted[0][1]["fingerprint"] == emitted[1][1]["fingerprint"]
    assert "private-provider-response" not in json.dumps(emitted)
    assert "sk-tr-v1-" not in json.dumps(emitted)


def test_delivery_failure_does_not_advance_cooldown(monkeypatch):
    state = {"sent": {}}
    def fail(*args):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(alerts, "send_sentry", fail)
    with pytest.raises(RuntimeError):
        alerts.report("refresh_failed", {}, state, NOW)
    assert state == {"sent": {}}


def test_unknown_step_name_is_not_exported(emitted):
    failed_jobs = jobs()
    failed_jobs[0]["steps"][0]["name"] = "sk-secret-not-a-reviewed-step-name"
    details = alerts.failed_run(run(), failed_jobs)
    assert details["stage"] == "other"
    assert "sk-secret" not in json.dumps(details)


def test_stale_and_failure_have_independent_bounded_groups(emitted):
    state = {"sent": {}}
    for kind in ("refresh_failed", "catalog_stale"):
        assert alerts.report(kind, {}, state, NOW)
        assert not alerts.report(kind, {}, state, NOW)
    assert len(emitted) == 2
    assert emitted[0][1]["fingerprint"] != emitted[1][1]["fingerprint"]


def test_watchdog_heartbeat_is_external_and_does_not_spend_error_events(emitted):
    alerts.heartbeat("in_progress", "a" * 32)
    alerts.heartbeat("ok", "a" * 32, 2.5)
    kind, payload = emitted[0]
    assert kind == "check_in"
    assert payload["monitor_slug"] == "price-refresh-watchdog"
    assert payload["monitor_config"]["checkin_margin"] == 60
    assert payload["monitor_config"]["schedule"] == {"type": "interval", "value": 1, "unit": "hour"}
    assert emitted[1][1]["check_in_id"] == payload["check_in_id"]
    assert emitted[1][1]["status"] == "ok"
    assert emitted[1][1]["duration"] == 2.5
    assert "monitor_config" not in emitted[1][1]


@pytest.fixture
def observer(monkeypatch, emitted):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(alerts, "datetime", Clock)
    monkeypatch.setenv("GITHUB_REPOSITORY", alerts.REPOSITORY)
    monkeypatch.delenv("TR_REFRESH_RUN_ID", raising=False)
    state = {"last_validated_at": None, "sent": {}}
    monkeypatch.setattr(alerts, "read_state", lambda: (123, copy.deepcopy(state)))
    calls = []
    data = {"runs": [run()], "jobs": jobs()}
    def fake_gh(path, payload=None):
        calls.append((path, payload))
        if path.startswith("actions/workflows/"):
            return {"workflow_runs": data["runs"]}
        if path.endswith("/jobs?per_page=100"):
            return {"total_count": 1, "jobs": data["jobs"]}
        if path.startswith("actions/runs/"):
            return data.get("trigger", run())
        if path == "issues/123":
            return {}
        raise AssertionError(path)
    monkeypatch.setattr(alerts, "gh", fake_gh)
    return state, data, calls


def test_failed_refresh_and_absent_freshness_report_and_persist(observer, emitted):
    _, _, calls = observer
    alerts.main()
    assert [item[0] for item in emitted] == ["check_in", "event", "event", "check_in"]
    write = calls[-1][1]
    assert write["state"] == "open"
    assert '"refresh_failed":' in write["body"]
    assert '"catalog_stale":' in write["body"]


def test_no_runs_still_alerts_stale(observer, emitted):
    _, data, _ = observer
    data["runs"] = []
    alerts.main()
    assert emitted[1][1]["fingerprint"] == ["price-refresh", "catalog_stale"]


def test_successful_recompute_without_publication_cannot_clear_staleness(observer, emitted):
    _, data, calls = observer
    data["runs"] = [run(conclusion="success")]
    data["jobs"] = []
    alerts.main()
    assert emitted[1][1]["fingerprint"] == ["price-refresh", "catalog_stale"]
    assert calls[-1][1]["state"] == "open"


def test_genuine_publication_closes_ledger_preserves_cooldown(observer, emitted):
    state, data, calls = observer
    state["sent"]["refresh_failed"] = (NOW - timedelta(hours=1)).isoformat()
    data["runs"] = [run(conclusion="success")]
    data["jobs"] = jobs(validated=True)
    alerts.main()
    assert calls[-1][1]["state"] == "closed"
    assert state["sent"]["refresh_failed"] in calls[-1][1]["body"]
    assert [kind for kind, _ in emitted] == ["check_in", "check_in"]


@pytest.mark.parametrize("age,stale", [(timedelta(hours=5, minutes=59), False), (timedelta(hours=6), True)])
def test_six_hour_freshness_boundary(observer, emitted, age, stale):
    state, data, _ = observer
    state["last_validated_at"] = (NOW - age).isoformat()
    data["runs"] = []
    alerts.main()
    assert any(kind == "event" for kind, _ in emitted) is stale


def test_dry_run_has_no_writes_or_sentry_events(observer, emitted, capsys):
    _, _, calls = observer
    alerts.main(dry_run=True)
    assert not emitted
    assert all(payload is None for _, payload in calls)
    assert json.loads(capsys.readouterr().out)["stale"]


def test_new_failure_is_reported_even_if_later_run_recovered(observer, emitted):
    state, data, _ = observer
    state["last_validated_at"] = NOW.isoformat()
    data["runs"] = [run(101, "success"), run(100, when=NOW - timedelta(hours=1))]
    alerts.main()
    assert emitted[1][1]["extra"]["run_id"] == 100


def test_recovered_old_failure_is_not_repeated_forever(observer, emitted):
    state, data, _ = observer
    state["last_validated_at"] = NOW.isoformat()
    state["last_failure"] = "100:1"
    data["runs"] = [run(101, "success"), run(100, when=NOW - timedelta(hours=1))]
    alerts.main()
    assert [kind for kind, _ in emitted] == ["check_in", "check_in"]


def test_api_outage_does_not_emit_false_healthy_heartbeat(observer, emitted, monkeypatch):
    def fail(*args):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(alerts, "gh", fail)
    with pytest.raises(RuntimeError):
        alerts.main()
    assert [payload["status"] for _, payload in emitted] == ["in_progress", "error"]


def test_future_ledger_is_rejected(observer):
    state, _, _ = observer
    state["sent"]["refresh_failed"] = (NOW + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="future"):
        alerts.main()


def test_ledger_search_includes_closed_issues_and_validates_owner(monkeypatch):
    state = {"last_validated_at": None, "sent": {}}
    record = {"title": alerts.ISSUE_TITLE, "number": 12,
              "user": {"login": "github-actions[bot]"},
              "body": alerts.STATE_MARKER + json.dumps(state) + " -->"}
    paths = []
    def fake(path):
        paths.append(path)
        return {"total_count": 1, "items": [record]}
    monkeypatch.setattr(alerts, "gh", fake)
    assert alerts.read_state() == (12, state)
    assert "is:open" not in paths[0] and "search/issues" in paths[0]
    record["user"]["login"] = "untrusted"
    with pytest.raises(ValueError, match="owned"):
        alerts.read_state()


def test_missing_or_incomplete_ledger_fails_closed(monkeypatch):
    monkeypatch.setattr(alerts, "gh", lambda _: {"total_count": 0, "items": [], "incomplete_results": True})
    with pytest.raises(ValueError, match="Incomplete"):
        alerts.read_state()


@pytest.mark.parametrize("dsn", ["", "http://abc@o1.ingest.sentry.io/2", "https://abc@example.com/2",
                                 "https://abc@o1.ingest.sentry.io/2?secret=1"])
def test_invalid_dsn_never_sends(monkeypatch, dsn):
    monkeypatch.setenv("TR_SENTRY_DSN", dsn)
    with pytest.raises(ValueError):
        alerts.send_sentry("event", {})


def test_envelope_is_synchronous_bounded_and_redirections_disabled(monkeypatch):
    monkeypatch.setenv("TR_SENTRY_DSN", "https://abc@o1.ingest.us.sentry.io/2")
    requests = []
    class Reply:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    class Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            return Reply()
    def opener(handler):
        assert handler.redirect_request() is None
        return Opener()
    monkeypatch.setattr(alerts, "build_opener", opener)
    alerts.send_sentry("event", {"message": "safe", "event_id": "a" * 32})
    request, timeout = requests[0]
    assert timeout == 15
    assert request.full_url == "https://o1.ingest.us.sentry.io/api/2/envelope/"
    header, item, payload = map(json.loads, request.data.splitlines())
    assert header["dsn"] == "https://abc@o1.ingest.us.sentry.io/2"
    assert header["event_id"] == "a" * 32
    assert item == {"type": "event"}
    assert payload == {"message": "safe", "event_id": "a" * 32}


def test_sentry_429_is_not_success(monkeypatch):
    monkeypatch.setenv("TR_SENTRY_DSN", "https://abc@o1.ingest.sentry.io/2")
    class Opener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 429, "quota", {}, None)
    monkeypatch.setattr(alerts, "build_opener", lambda _: Opener())
    with pytest.raises(RuntimeError, match="cooldown not advanced"):
        alerts.send_sentry("event", {"event_id": "a" * 32})


def test_cli_failure_does_not_echo_output(monkeypatch):
    monkeypatch.setattr(alerts.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args, 1, "secret", "secret"))
    with pytest.raises(RuntimeError, match="^GitHub monitoring API request failed$"):
        alerts.gh("issues")


def test_independent_workflow_always_runs_trusted_main_without_dependencies():
    workflow = yaml.safe_load((ROOT / ".github/workflows/price-refresh-alerts.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["workflow_run"] == {
        "workflows": ["Refresh upstream prices"], "branches": ["main"], "types": ["completed"],
    }
    assert triggers["schedule"]
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["permissions"] == {"contents": "read", "actions": "read", "issues": "write"}
    steps = workflow["jobs"]["report"]["steps"]
    assert steps[0]["with"]["ref"] == "main"
    assert steps[0]["with"]["persist-credentials"] is False
    assert steps[1]["run"] == "python3 scripts/price_refresh_alerts.py"
    assert "PRICE_REFRESH_SENTRY_DSN" in steps[1]["env"]["TR_SENTRY_DSN"]


def test_publication_marker_only_follows_validated_output():
    workflow = yaml.safe_load((ROOT / ".github/workflows/refresh-prices.yml").read_text())
    steps = workflow["jobs"]["refresh"]["steps"]
    publication = next(step for step in steps if step.get("id") == "publication")
    marker = next(step for step in steps if step.get("name") == alerts.MARKER_STEP)
    assert marker["if"] == "steps.publication.outputs.validated == 'true'"
    assert steps.index(publication) < steps.index(marker)
    assert publication["run"].count('echo "validated=true"') == 2
