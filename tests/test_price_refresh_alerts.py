import copy
import http.client
import io
import json
import runpy
import ssl
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError

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
    # The timer starts within about 40 s of :23. With threshold 2, a 15-minute
    # margin pages a silent host about 2 h 15 min after its last check-in.
    assert payload["monitor_config"]["checkin_margin"] == 15
    assert payload["monitor_config"]["schedule"] == {"type": "interval", "value": 1, "unit": "hour"}
    # Two consecutive bad hours page; a single transient failure does not.
    assert payload["monitor_config"]["failure_issue_threshold"] == 2
    assert payload["monitor_config"]["recovery_threshold"] == 1
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
        if path.endswith("/dispatches"):
            if data.get("dispatch_error"):
                raise RuntimeError("GitHub monitoring API request failed")
            return None
        if path.startswith("actions/workflows/") and "status=completed" not in path:
            wanted = path.split("&status=", 1)[1] if "&status=" in path else None
            # "beyond_page" marks a run older than the 20 newest: only a
            # status-filtered query returns it.
            return {"workflow_runs": [item for item in data.get("recent", [])
                                      if (wanted is None and not item.get("beyond_page"))
                                      or wanted == item["status"]]}
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


def recent(status="completed", *, created=NOW - timedelta(hours=3), started=None, run_id=200):
    return {**run(run_id, "success" if status == "completed" else None), "status": status,
            "created_at": created.isoformat(),
            "run_started_at": (started or created).isoformat()}


def dispatches(calls):
    return [(path, payload) for path, payload in calls if path.endswith("/dispatches")]


def test_stalled_schedule_dispatches_refresh_on_main_and_records_it(observer, emitted):
    state, data, calls = observer
    state["last_validated_at"] = (NOW - timedelta(hours=2)).isoformat()
    data["runs"] = []
    data["recent"] = [recent(created=NOW - timedelta(minutes=91))]
    alerts.main(dispatch=True)
    assert dispatches(calls) == [("actions/workflows/refresh-prices.yml/dispatches", {"ref": "main"})]
    assert f'"refresh_dispatch": "{NOW.isoformat()}"' in calls[-1][1]["body"]
    # Fresh enough: dispatching alone is not an alert.
    assert [kind for kind, _ in emitted] == ["check_in", "check_in"]


def test_stale_catalog_dispatches_and_still_alerts(observer, emitted):
    _, data, calls = observer
    data["runs"] = []
    alerts.main(dispatch=True)
    assert len(dispatches(calls)) == 1
    assert [payload["fingerprint"] for kind, payload in emitted if kind == "event"] == [
        ["price-refresh", "catalog_stale"]]
    assert calls[-1][1]["state"] == "open"


@pytest.mark.parametrize("status,beyond_page", [
    ("queued", True), ("in_progress", True),
    ("waiting", False), ("requested", False), ("pending", False),
])
def test_no_dispatch_while_a_refresh_is_active(observer, status, beyond_page):
    _, data, calls = observer
    data["runs"] = []
    # A rerun of an old run keeps its created_at, so it can fall outside the
    # newest page; refresh_runs asks for queued and in_progress runs too.
    data["recent"] = [{**recent(status, created=NOW - timedelta(days=2)), "beyond_page": beyond_page}]
    alerts.main(dispatch=True)
    assert not dispatches(calls)


def test_no_dispatch_after_a_recent_start_or_rerun(observer):
    _, data, calls = observer
    data["runs"] = []
    data["recent"] = [recent(created=NOW - timedelta(days=1), started=NOW - timedelta(minutes=89))]
    alerts.main(dispatch=True)
    assert not dispatches(calls)


def test_no_second_dispatch_inside_the_cooldown(observer):
    state, data, calls = observer
    data["runs"] = []
    state["sent"]["refresh_dispatch"] = (NOW - timedelta(minutes=89)).isoformat()
    alerts.main(dispatch=True)
    assert not dispatches(calls)
    state["sent"]["refresh_dispatch"] = (NOW - timedelta(minutes=90)).isoformat()
    alerts.main(dispatch=True)
    assert len(dispatches(calls)) == 1


def test_dispatch_is_opt_in(observer):
    _, data, calls = observer
    data["runs"] = []
    alerts.main()
    assert not dispatches(calls)
    assert not any("status=queued" in path for path, _ in calls)


def test_dispatch_dry_run_makes_no_writes(observer, emitted, capsys):
    _, data, calls = observer
    data["runs"] = []
    alerts.main(dry_run=True, dispatch=True)
    assert not emitted
    assert all(payload is None for _, payload in calls)
    assert json.loads(capsys.readouterr().out)["dispatch"] == "dispatch"


def test_dispatch_failure_is_reported_and_freshness_alert_still_fires(observer, emitted, capsys):
    state, data, calls = observer
    data["runs"] = []
    data["dispatch_error"] = True
    alerts.main(dispatch=True)
    fingerprints = [payload["fingerprint"] for kind, payload in emitted if kind == "event"]
    assert fingerprints == [["price-refresh", "catalog_stale"],
                            ["price-refresh", "refresh_dispatch_failed"]]
    assert emitted[-1] == ("check_in", emitted[-1][1]) and emitted[-1][1]["status"] == "ok"
    body = calls[-1][1]["body"]
    assert '"refresh_dispatch":' not in body  # a failed attempt is not a dispatch
    assert calls[-1][1]["state"] == "open"
    assert "Price refresh dispatch failed (RuntimeError)" in capsys.readouterr().err


def test_anonymous_host_observer_cannot_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_REPOSITORY", alerts.REPOSITORY)
    monkeypatch.setenv("TR_PRICE_REFRESH_PUBLIC_API", "1")
    with pytest.raises(ValueError, match="cannot dispatch"):
        alerts.main(state_path=tmp_path / "state.json", dispatch=True)


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
    assert "author:github-actions%5Bbot%5D" in paths[0]
    # Anyone can open an issue in this public repo. A copied bot title must
    # neither control the ledger nor disable notifications with a duplicate.
    record["user"]["login"] = "untrusted"
    assert alerts.read_state() == (None, state)


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


class _Reply:
    """A response whose body is bytes, or an exception raised by read()."""

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body[:limit]


class _BodyFails:
    """The headers arrive, then reading the body raises this exception."""

    def __init__(self, exc):
        self.exc = exc


def _framed(raw):
    """A real http.client response parsed from raw HTTP/1.1 bytes."""
    class Socket:
        def makefile(self, *_args, **_kwargs):
            return io.BytesIO(raw)
    response = http.client.HTTPResponse(Socket())
    response.begin()
    return response


START = 1000.0


@pytest.fixture
def read_clock():
    """The monotonic clock public_reads drives: sleeps and attempt I/O advance it."""
    return {"now": START, "attempt": 0.0, "costs": []}


@pytest.fixture
def public_reads(monkeypatch, read_clock):
    """Scripted anonymous GitHub replies: bytes, an exception from open(), _BodyFails, or
    a real HTTPResponse. Each attempt costs ``read_clock["attempt"]`` seconds of I/O, or
    the next value of ``read_clock["costs"]`` when that list is not empty."""
    script, sleeps, opened = [], [], []
    clock = read_clock
    class Opener:
        def open(self, request, timeout):
            opened.append((request.full_url, clock["now"]))
            clock["now"] += clock["costs"].pop(0) if clock["costs"] else clock["attempt"]
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            if isinstance(item, http.client.HTTPResponse):
                return item
            return _Reply(item.exc if isinstance(item, _BodyFails) else item)
    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds
    monkeypatch.setattr(alerts, "build_opener", lambda _: Opener())
    monkeypatch.setattr(alerts.time, "sleep", sleep)
    monkeypatch.setattr(alerts.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(alerts, "_run_started", None, raising=False)
    return script, sleeps, opened


def _http_error(code, headers=None):
    return HTTPError("https://api.github.com/x", code, "error", headers or {}, None)


def test_public_read_retries_a_transient_server_error(public_reads):
    script, sleeps, opened = public_reads
    script += [_http_error(502), b'{"workflow_runs": []}']
    assert alerts.public_gh("actions/workflows/refresh-prices.yml/runs") == {"workflow_runs": []}
    assert sleeps == [2.0]
    assert len(opened) == 2


def test_public_read_gives_up_after_bounded_retries_with_a_safe_reason(public_reads):
    script, sleeps, opened = public_reads
    script += [URLError(TimeoutError("timed out"))] * 3
    with pytest.raises(alerts.MonitoringReadError) as raised:
        alerts.public_gh("actions/workflows/refresh-prices.yml/runs")
    assert str(raised.value) == "GitHub monitoring API request failed (timeout)"
    assert sleeps == list(alerts.PUBLIC_READ_RETRY_DELAYS)
    assert len(opened) == 3


@pytest.mark.parametrize("value", ["5 ", " 5", "\t5\t"])
def test_public_read_honors_a_padded_retry_after(public_reads, value):
    script, sleeps, _ = public_reads
    script += [_http_error(429, {"retry-after": value}), b"{}"]
    assert alerts.public_gh("actions/runs/1") == {}
    assert sleeps == [5.0]


def test_public_read_backoff_is_the_floor_under_a_tiny_retry_after(public_reads):
    script, sleeps, _ = public_reads
    script += [_http_error(429, {"retry-after": "1"})] * 2 + [b"{}"]
    assert alerts.public_gh("actions/runs/1") == {}
    assert sleeps == [2.0, 8.0]


def test_public_read_does_not_retry_a_certificate_failure(public_reads):
    script, sleeps, opened = public_reads
    script.append(URLError(ssl.SSLCertVerificationError(1, "certificate has expired")))
    with pytest.raises(alerts.MonitoringReadError,
                       match=r"^GitHub monitoring API request failed \(TLS certificate verification failed\)$"):
        alerts.public_gh("actions/runs/1")
    assert sleeps == [] and len(opened) == 1


@pytest.mark.parametrize("error", [
    _http_error(502), URLError(TimeoutError("timed out")), _http_error(429, {"retry-after": "1"}),
], ids=["502", "timeout", "short-rate-limit"])
def test_the_dispatch_post_is_never_retried(public_reads, monkeypatch, error):
    # A dispatch that timed out may already have queued a run; a second one
    # could cancel it (refresh-prices keeps one pending run per group).
    script, sleeps, opened = public_reads
    monkeypatch.setattr(alerts, "_github_token", "github_pat_test", raising=False)
    script += [error, b""]
    with pytest.raises(alerts.MonitoringReadError):
        alerts.public_gh(alerts.DISPATCH_PATH, {"ref": "main"})
    assert sleeps == [] and len(opened) == 1


def test_a_dispatch_answering_204_returns_none(public_reads, monkeypatch):
    script, _, _ = public_reads
    monkeypatch.setattr(alerts, "_github_token", "github_pat_test", raising=False)
    script.append(b"")
    assert alerts.public_gh(alerts.DISPATCH_PATH, {"ref": "main"}) is None


def test_each_attempt_gets_a_fresh_https_request(monkeypatch):
    # urllib's ProxyHandler rewrites the Request it is given (set_proxy turns an
    # https request into an http one aimed at the proxy). Reusing one Request
    # across retries sent the third attempt over plain HTTP.
    seen = []
    class Opener:
        def open(self, request, timeout):
            seen.append((request, request.type, request.host))
            request.set_proxy("proxy.invalid:8080", "http")
            if len(seen) < 3:
                raise _http_error(502)
            return _Reply(b"{}")
    monkeypatch.setattr(alerts, "build_opener", lambda _: Opener())
    monkeypatch.setattr(alerts.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(alerts, "_run_started", None, raising=False)
    assert alerts.public_gh("actions/runs/1") == {}
    assert [(kind, host) for _, kind, host in seen] == [("https", "api.github.com")] * 3
    assert len({id(request) for request, _, _ in seen}) == 3


@pytest.mark.parametrize("error", [
    ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"),
    ssl.SSLError(1, "[SSL: UNSUPPORTED_PROTOCOL] unsupported protocol"),
])
def test_public_read_does_not_retry_a_tls_negotiation_failure(public_reads, error):
    script, sleeps, opened = public_reads
    script.append(URLError(error))
    with pytest.raises(alerts.MonitoringReadError,
                       match=r"^GitHub monitoring API request failed \(TLS error\)$"):
        alerts.public_gh("actions/runs/1")
    assert sleeps == [] and len(opened) == 1


@pytest.mark.parametrize("error", [
    ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
    ssl.SSLZeroReturnError(6, "TLS/SSL connection has been closed"),
])
def test_public_read_retries_a_tls_connection_that_ended(public_reads, error):
    script, sleeps, _ = public_reads
    script += [URLError(error), b"{}"]
    assert alerts.public_gh("actions/runs/1") == {}
    assert sleeps == [2.0]


def test_public_read_honors_a_short_retry_after(public_reads):
    script, sleeps, _ = public_reads
    script += [_http_error(429, {"retry-after": "5"}), b"{}"]
    assert alerts.public_gh("actions/runs/1") == {}
    assert sleeps == [5.0]


@pytest.mark.parametrize("error", [
    _http_error(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"}),
    _http_error(429, {"retry-after": "3600"}),
])
def test_public_read_does_not_sleep_through_a_long_rate_limit(public_reads, error):
    script, sleeps, opened = public_reads
    script.append(error)
    with pytest.raises(alerts.MonitoringReadError,
                       match=rf"^GitHub monitoring API request failed \(HTTP {error.code}, rate limited\)$"):
        alerts.public_gh("actions/runs/1")
    assert sleeps == []
    assert len(opened) == 1


@pytest.mark.parametrize("code", [401, 403, 404, 422])
def test_public_read_does_not_retry_a_client_error(public_reads, code):
    script, sleeps, opened = public_reads
    script.append(_http_error(code))
    with pytest.raises(alerts.MonitoringReadError, match=rf"\(HTTP {code}\)$"):
        alerts.public_gh("actions/runs/1")
    assert sleeps == [] and len(opened) == 1


@pytest.mark.parametrize("exc", [
    ConnectionResetError(104, "Connection reset by peer"),
    http.client.IncompleteRead(b"{", 10),
    http.client.RemoteDisconnected("Remote end closed connection without response"),
], ids=["reset", "incomplete-read", "remote-disconnected"])
def test_public_read_retries_a_body_that_fails_mid_read(public_reads, exc):
    script, sleeps, opened = public_reads
    script += [_BodyFails(exc), b"{}"]
    assert alerts.public_gh("actions/runs/1") == {}
    assert sleeps == [2.0] and len(opened) == 2


def test_public_read_body_failure_that_persists_is_a_safe_monitoring_error(public_reads):
    script, _, _ = public_reads
    script += [_BodyFails(http.client.IncompleteRead(b"secret-partial-body", 99))] * 3
    with pytest.raises(alerts.MonitoringReadError) as raised:
        alerts.public_gh("actions/runs/1")
    assert str(raised.value) == "GitHub monitoring API request failed (network error)"


def test_retries_share_one_window_per_run(public_reads, read_clock):
    # Every read times out until the run's retry window is spent; then the next
    # failed read fails the run instead of retrying past the unit's timeout.
    script, sleeps, opened = public_reads
    read_clock["attempt"] = 1.0
    alerts.start_run_budget()
    script += [URLError(TimeoutError("timed out")), b"{}"] * 100
    reads = 0
    with pytest.raises(alerts.MonitoringReadError, match=r"\(timeout\)$"):
        while True:
            alerts.public_gh("actions/runs/1")
            reads += 1
    assert reads > 0 and len(sleeps) == reads
    # A retry is taken only while its sleep plus one slow attempt fits in the
    # window, so no retry attempt starts past it.
    retry_starts = [started for _, started in opened[1::2]]
    window_end = START + alerts.PUBLIC_READ_RETRY_WINDOW_SECONDS
    assert max(retry_starts) + alerts.PUBLIC_READ_ATTEMPT_ALLOWANCE_SECONDS <= window_end


def test_slow_reads_stop_at_the_run_deadline(public_reads, read_clock):
    # Review round 2: two 30-second Retry-After replies on the first read, then
    # 21 slow successful reads. Without a deadline the reads end at 9:24; now no
    # attempt starts unless one slow attempt still fits before the deadline.
    script, sleeps, opened = public_reads
    read_clock["costs"] = [28.0, 28.0, 28.0]
    read_clock["attempt"] = 20.0
    alerts.start_run_budget()
    script += [_http_error(429, {"retry-after": "30"})] * 2 + [b"{}"] * 22
    with pytest.raises(alerts.MonitoringReadError,
                       match=r"^GitHub monitoring API request failed \(run deadline reached\)$"):
        for _ in range(22):
            alerts.public_gh("actions/runs/1")
    assert sleeps == [30.0, 30.0]
    deadline = START + alerts.PUBLIC_READ_DEADLINE_SECONDS
    assert max(started for _, started in opened) + alerts.PUBLIC_READ_ATTEMPT_ALLOWANCE_SECONDS <= deadline
    assert read_clock["now"] <= deadline


def test_a_normal_run_is_far_from_the_deadline(public_reads, read_clock):
    script, _, opened = public_reads
    read_clock["attempt"] = 1.0
    alerts.start_run_budget()
    script += [b"{}"] * 22
    for _ in range(22):
        assert alerts.public_gh("actions/runs/1") == {}
    assert len(opened) == 22


def test_main_starts_a_fresh_run_budget_each_run(observer, monkeypatch):
    runs = []
    monkeypatch.setattr(alerts, "start_run_budget", lambda: runs.append(True))
    alerts.main(heartbeat_enabled=False)
    alerts.main(dry_run=True)
    assert runs == [True, True]


def test_a_body_short_of_content_length_is_retried(public_reads):
    # read(n) on a real response returns the partial body without raising when
    # the connection closes before Content-Length.
    script, sleeps, opened = public_reads
    script += [_framed(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n{"), b"{}"]
    assert alerts.public_gh("actions/runs/1") == {}
    assert sleeps == [2.0] and len(opened) == 2


@pytest.mark.parametrize("raw", [
    # Optional whitespace after a field value is valid HTTP (RFC 9112).
    b"HTTP/1.1 200 OK\r\nContent-Length: 10 \r\n\r\n{}",
    b"HTTP/1.1 200 OK\r\nContent-Length:  10\t\r\n\r\n{",
], ids=["trailing-space", "padded"])
def test_a_short_body_is_detected_however_content_length_is_padded(public_reads, raw):
    script, sleeps, _ = public_reads
    script += [_framed(raw), b"{}"]
    assert alerts.public_gh("actions/runs/1") == {}
    assert sleeps == [2.0]


def test_an_unparseable_content_length_defers_to_json_validation(public_reads):
    # http.client ignores a non-numeric Content-Length (length is None); the body
    # is then judged as JSON, exactly as for a close-delimited response.
    script, sleeps, _ = public_reads
    script.append(_framed(b"HTTP/1.1 200 OK\r\nContent-Length: \xc2\xb2\r\n\r\n{}"))
    assert alerts.public_gh("actions/runs/1") == {}
    assert sleeps == []


def test_a_persistently_short_body_is_a_safe_monitoring_error(public_reads):
    script, _, _ = public_reads
    script += [_framed(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n{") for _ in range(3)]
    with pytest.raises(alerts.MonitoringReadError,
                       match=r"^GitHub monitoring API request failed \(network error\)$"):
        alerts.public_gh("actions/runs/1")


@pytest.mark.parametrize("raw", [
    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}",
    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n",
    b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n{}",
], ids=["content-length", "chunked", "close-delimited"])
def test_a_complete_framed_body_is_not_retried(public_reads, raw):
    script, sleeps, opened = public_reads
    script.append(_framed(raw))
    assert alerts.public_gh("actions/runs/1") == {}
    assert sleeps == [] and len(opened) == 1


def test_cli_prints_the_safe_read_failure_reason(monkeypatch, tmp_path, capsys):
    # Drive the real __main__ handler: a public read that fails with 404.
    def build_opener(*_handlers):
        class Opener:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 404, "secret-reason", {}, None)
        return Opener()
    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    monkeypatch.setenv("GITHUB_REPOSITORY", alerts.REPOSITORY)
    monkeypatch.setenv("TR_PRICE_REFRESH_PUBLIC_API", "1")
    monkeypatch.setattr(sys, "argv", ["price_refresh_alerts.py", "--no-heartbeat",
                                      "--state-file", str(tmp_path / "state.json")])
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(ROOT / "scripts/price_refresh_alerts.py"), run_name="__main__")
    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert err.strip() == "::error::Price refresh monitoring failed: GitHub monitoring API request failed (HTTP 404)"


def test_public_read_failure_message_never_carries_the_url(public_reads):
    script, _, _ = public_reads
    script += [URLError("name resolution failed for api.github.com")] * 3
    with pytest.raises(alerts.MonitoringReadError) as raised:
        alerts.public_gh("actions/runs/1")
    assert str(raised.value) == "GitHub monitoring API request failed (network error)"
    assert "://" not in str(raised.value) and "github.com" not in str(raised.value)


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
    # actions: write only so the observer can dispatch a dropped refresh-prices run.
    assert workflow["permissions"] == {"contents": "read", "actions": "write", "issues": "write"}
    steps = workflow["jobs"]["report"]["steps"]
    assert steps[0]["with"]["ref"] == "main"
    assert steps[0]["with"]["persist-credentials"] is False
    assert steps[1]["run"] == "python3 scripts/price_refresh_alerts.py --no-heartbeat --dispatch-stalled"
    assert "PRICE_REFRESH_SENTRY_DSN" in steps[1]["env"]["TR_SENTRY_DSN"]


def test_publication_marker_only_follows_validated_output():
    workflow = yaml.safe_load((ROOT / ".github/workflows/refresh-prices.yml").read_text())
    steps = workflow["jobs"]["refresh"]["steps"]
    publication = next(step for step in steps if step.get("id") == "publication")
    marker = next(step for step in steps if step.get("name") == alerts.MARKER_STEP)
    assert marker["if"] == "steps.publication.outputs.validated == 'true'"
    assert steps.index(publication) < steps.index(marker)
    # No change, an exact-SHA publication, an accepted carried publication.
    assert publication["run"].count('echo "validated=true"') == 3
