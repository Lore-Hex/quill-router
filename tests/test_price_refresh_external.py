import json
import os
import subprocess
import sys
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError

import pytest

from scripts import price_refresh_alerts as alerts


def test_public_reader_has_no_credentials_or_redirects(monkeypatch):
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            assert limit == 2_000_001
            return b'{"workflow_runs": []}'

    class Opener:
        def open(self, request, timeout):
            assert timeout == 15
            seen.append(request)
            return Response()

    def opener(handler):
        assert handler.redirect_request() is None
        return Opener()

    monkeypatch.setenv("GH_TOKEN", "must-not-leave-this-process")
    monkeypatch.setattr(alerts, "build_opener", opener)
    result = alerts.public_gh("actions/workflows/refresh-prices.yml/runs?per_page=20")
    assert result == {"workflow_runs": []}
    assert seen[0].full_url.startswith("https://api.github.com/repos/Lore-Hex/quill-router/")
    assert "Authorization" not in dict(seen[0].header_items())
    assert "must-not-leave" not in repr(seen[0].header_items())


@pytest.mark.parametrize("path", ["https://evil.invalid", "../other", "/user", "repos/other/repo/actions"])
def test_public_reader_rejects_out_of_scope_paths(path):
    with pytest.raises(ValueError):
        alerts.public_gh(path)


def test_local_state_merges_cooldowns_without_changing_remote_ledger(tmp_path):
    now = datetime(2026, 11, 3, tzinfo=UTC)
    old = (now - timedelta(hours=2)).isoformat()
    recent = (now - timedelta(hours=1)).isoformat()
    path = tmp_path / "state.json"
    alerts.write_local_state(path, {"last_validated_at": recent, "sent": {"refresh_failed": old}, "last_failure": "12:1"})
    remote = {"last_validated_at": old, "sent": {"refresh_failed": recent, "catalog_stale": old}}
    state = alerts.read_local_state(path, remote)
    assert state == {"last_validated_at": recent, "sent": {"refresh_failed": recent, "catalog_stale": old}, "last_failure": "12:1"}
    assert remote["last_validated_at"] == old
    assert path.stat().st_mode & 0o777 == 0o600


def test_corrupt_local_state_fails_closed(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"sent": null}')
    with pytest.raises(ValueError):
        alerts.read_local_state(path, {"last_validated_at": None, "sent": {}})


def test_local_state_replace_failure_preserves_previous_ledger(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    prior = {"last_validated_at": None, "sent": {}}
    alerts.write_local_state(path, prior)

    def fail(*args):
        raise OSError("disk error")

    monkeypatch.setattr(alerts.os, "replace", fail)
    with pytest.raises(OSError):
        alerts.write_local_state(path, {"last_validated_at": None, "sent": {"catalog_stale": "2026-11-03T00:00:00Z"}})
    assert json.loads(path.read_text()) == prior
    assert list(tmp_path.iterdir()) == [path]


def test_local_lock_prevents_two_observers_from_running(tmp_path):
    path = tmp_path / "state.json"
    with alerts.local_state_lock(path) as acquired:
        assert acquired
        with alerts.local_state_lock(path) as second:
            assert not second


def test_external_observer_persists_without_github_writes(tmp_path, monkeypatch):
    now = datetime.now(UTC).isoformat()
    state = {"last_validated_at": now, "sent": {}}
    monkeypatch.setattr(alerts, "read_state", lambda: (123, state))
    calls = []

    def read(path, data=None):
        assert data is None, "external observer must not mutate GitHub"
        calls.append(path)
        return {"workflow_runs": []}

    monkeypatch.setattr(alerts, "gh", read)
    path = tmp_path / "state.json"
    alerts.observe(state_path=path)
    assert calls == ["actions/workflows/refresh-prices.yml/runs?branch=main&status=completed&per_page=20"]
    assert json.loads(path.read_text())["last_validated_at"] == now


def test_external_state_write_failure_cannot_send_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", alerts.REPOSITORY)
    monkeypatch.setattr(alerts, "read_state", lambda: (123, {"last_validated_at": datetime.now(UTC).isoformat(), "sent": {}}))
    monkeypatch.setattr(alerts, "gh", lambda *args: {"workflow_runs": []})
    events = []
    monkeypatch.setattr(alerts, "heartbeat", lambda status, *args: events.append(status))

    def fail(*args):
        raise OSError("disk error")

    monkeypatch.setattr(alerts, "write_local_state", fail)
    with pytest.raises(OSError):
        alerts.main(state_path=tmp_path / "state.json")
    assert events == ["in_progress", "error"]


def test_public_mode_requires_local_state(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", alerts.REPOSITORY)
    monkeypatch.setenv("TR_PRICE_REFRESH_PUBLIC_API", "1")
    with pytest.raises(ValueError, match="local state"):
        alerts.main()


def test_public_mode_cannot_mutate_github(monkeypatch):
    monkeypatch.setenv("TR_PRICE_REFRESH_PUBLIC_API", "1")
    with pytest.raises(ValueError, match="cannot write"):
        alerts.gh("issues/123", {"state": "closed"})


def test_public_api_rate_limit_is_a_failed_observation(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 429, "rate limited", {}, None)

    monkeypatch.setattr(alerts, "build_opener", lambda *args: Opener())
    with pytest.raises(RuntimeError, match="monitoring API request failed"):
        alerts.public_gh("actions/workflows/refresh-prices.yml/runs?per_page=20")


def test_no_heartbeat_mode_still_observes_and_reports(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", alerts.REPOSITORY)
    observations = []
    monkeypatch.setattr(alerts, "observe", lambda **kwargs: observations.append(kwargs))
    monkeypatch.setattr(alerts, "heartbeat", lambda *args: pytest.fail("workflow must not mask external observer outage"))
    alerts.main(heartbeat_enabled=False)
    # Dispatching a stalled refresh is opt-in (--dispatch-stalled), never implied.
    assert observations == [{"state_path": None, "dispatch": False}]


def test_dry_run_does_not_write_state_or_send_heartbeats(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", alerts.REPOSITORY)
    monkeypatch.setattr(alerts, "read_state", lambda: (123, {"last_validated_at": None, "sent": {}}))
    monkeypatch.setattr(alerts, "gh", lambda *args: {"workflow_runs": []})
    monkeypatch.setattr(alerts, "send_sentry", lambda *args: pytest.fail("dry run must not send"))
    alerts.main(dry_run=True, state_path=tmp_path / "state.json")
    assert list(tmp_path.iterdir()) == []


def test_service_is_restricted_and_independent_of_github():
    root = Path(__file__).resolve().parents[1]
    service = (root / "scripts/ops/price-refresh-observer.service").read_text()
    for required in (
        "DynamicUser=yes", "NoNewPrivileges=yes", "ProtectSystem=strict",
        "ProtectHome=yes", "PrivateTmp=yes", "MemoryMax=96M", "CPUQuota=10%",
        "TimeoutStartSec=9min", "StateDirectoryMode=0700", "CapabilityBoundingSet=",
        "LoadCredential=sentry-dsn:/etc/tr-price-refresh/ingest.dsn",
        "IPAddressDeny=169.254.169.254/32", "TR_PRICE_REFRESH_PUBLIC_API=1",
    ):
        assert required in service
    assert "GH_TOKEN" not in service
    assert "--no-heartbeat" not in service
    # The host was anonymous and could not dispatch. GitHub keeps dropping the
    # refresh cron and the workflow observer's own stall-dispatch rides the
    # same scheduler, so the host now dispatches too, with a fine-grained
    # token passed as a systemd credential: a file path in argv, never the
    # value, and never an environment variable.
    assert "LoadCredential=github-token:/etc/tr-price-refresh/github-dispatch.token" in service
    exec_start = next(line for line in service.splitlines() if line.startswith("ExecStart="))
    assert "--github-token-file %d/github-token" in exec_start
    assert "--dispatch-stalled" in exec_start
    assert "github_pat_" not in service
    timer = (root / "scripts/ops/price-refresh-observer.timer").read_text()
    assert "OnCalendar=*-*-* *:23:00 UTC" in timer
    assert "Persistent=true" in timer
    installer = (root / "scripts/ops/install_price_refresh_observer.sh").read_text()
    assert 'git archive "$sha"' in installer
    assert 'git merge-base --is-ancestor "$sha" origin/main' in installer
    assert "tr-ops-local.json" in installer
    assert "add-iam-policy-binding" not in installer


def test_metadata_isolation_uses_loopback_dns_without_changing_host_resolver():
    root = Path(__file__).resolve().parents[1]
    service = (root / "scripts/ops/price-refresh-observer.service").read_text()
    assert "BindReadOnlyPaths=/run/systemd/resolve/stub-resolv.conf:/etc/resolv.conf" in service
    assert "Requires=systemd-resolved.service" in service
    assert "After=network-online.target systemd-resolved.service" in service
    assert "IPAddressDeny=169.254.169.254/32" in service
    assert "IPAddressDeny=fd20:ce::254/128" in service
    assert "IPAddressAllow=" not in service
    installer = (root / "scripts/ops/install_price_refresh_observer.sh").read_text()
    assert "systemctl is-active --quiet systemd-resolved.service" in installer
    assert "test -r /run/systemd/resolve/stub-resolv.conf" in installer


def test_installer_targets_an_existing_cluster_host_by_parameter():
    """roach-gcp was deleted with the observer still on it (2026-11-11)."""
    root = Path(__file__).resolve().parents[1]
    installer = (root / "scripts/ops/install_price_refresh_observer.sh").read_text()
    assert "host=${OBSERVER_HOST:-tr-clickhouse-3}" in installer
    assert "zone=${OBSERVER_ZONE:-us-central1-c}" in installer
    nodes = (root / "scripts/deploy/_clickhouse_publisher.sh").read_text()
    assert '"tr-clickhouse-3:us-central1-c"' in nodes
    commands = [line for line in installer.splitlines() if not line.lstrip().startswith("#")]
    assert not any("roach" in line for line in commands)
    assert 'compute ssh "$host" --zone="$zone"' in installer
    assert '"$host:$remote/"' in installer
    assert "sys.exit(sys.version_info < (3, 11))" in installer
    runbook = (root / "docs/clickhouse-reliability.md").read_text()
    assert "price-refresh-observer.timer" in runbook


TOKEN = "github_pat_" + "T0kenValueForTestsOnly_" * 4


def test_token_file_is_stripped_and_validated(tmp_path):
    path = tmp_path / "github-token"
    path.write_text(f"  {TOKEN}\n")
    assert alerts.read_github_token(path) == TOKEN
    secret = "ghp_" + "S3cretClassicToken" * 3
    for bad in ("", secret, f"{TOKEN}\n{TOKEN}", f"{TOKEN} extra", TOKEN + "x" * 300):
        path.write_text(bad)
        with pytest.raises(ValueError) as caught:
            alerts.read_github_token(path)
        assert TOKEN not in str(caught.value) and secret not in str(caught.value)


class FakeGitHub:
    """api.github.com for the host observer: a stalled refresh, no ledger."""

    def __init__(self, *, dispatch_status=204):
        self.requests = []
        self.dispatch_status = dispatch_status
        old = datetime.now(UTC) - timedelta(hours=3)
        self.old_run = {
            "id": 7, "run_attempt": 1, "head_sha": "a" * 40, "status": "completed",
            "conclusion": "success", "path": ".github/workflows/refresh-prices.yml",
            "head_branch": "main", "repository": {"full_name": alerts.REPOSITORY},
            "event": "schedule", "created_at": old.isoformat(),
            "run_started_at": old.isoformat(), "updated_at": old.isoformat(),
        }

    def __call__(self, handler):
        assert isinstance(handler, alerts.NoRedirect)
        return self

    def open(self, request, timeout):
        self.requests.append(request)
        url = request.full_url
        if request.get_method() == "POST":
            assert url.endswith("/actions/workflows/refresh-prices.yml/dispatches")
            if self.dispatch_status != 204:
                raise HTTPError(url, self.dispatch_status, "denied", {}, None)
            return self.Response(b"")
        if "/search/issues" in url:
            return self.Response(b'{"total_count": 0, "incomplete_results": false, "items": []}')
        if "status=completed" in url or "status=" in url:
            return self.Response(b'{"workflow_runs": []}')
        return self.Response(json.dumps({"workflow_runs": [self.old_run]}).encode())

    class Response:
        def __init__(self, raw):
            self.raw = raw

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return self.raw


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", alerts.REPOSITORY)
    monkeypatch.setenv("TR_PRICE_REFRESH_PUBLIC_API", "1")
    sent = []
    monkeypatch.setattr(alerts, "send_sentry", lambda kind, payload: sent.append((kind, payload)))
    return sent


def test_host_observer_with_token_dispatches_a_stalled_refresh(tmp_path, monkeypatch, host, capsys):
    github = FakeGitHub()
    monkeypatch.setattr(alerts, "build_opener", github)
    alerts.main(state_path=tmp_path / "state.json", dispatch=True, github_token=TOKEN)
    posts = [request for request in github.requests if request.get_method() == "POST"]
    assert len(posts) == 1
    assert json.loads(posts[0].data) == {"ref": "main"}
    # Every read is authenticated too: anonymous reads are limited to 60 an hour.
    assert all(request.get_header("Authorization") == f"Bearer {TOKEN}"
               for request in github.requests)
    assert all(request.full_url.startswith("https://api.github.com/repos/Lore-Hex/quill-router/")
               or request.full_url.startswith("https://api.github.com/search/issues?q=repo:Lore-Hex/quill-router")
               for request in github.requests)
    assert "refresh_dispatch" in json.loads((tmp_path / "state.json").read_text())["sent"]
    output = capsys.readouterr()
    assert "Dispatched refresh-prices.yml" in output.out
    assert TOKEN not in output.out + output.err + json.dumps(host)
    assert alerts._github_token is None


def test_host_observer_without_token_stays_anonymous_and_refuses(tmp_path, monkeypatch, host):
    github = FakeGitHub()
    monkeypatch.setattr(alerts, "build_opener", github)
    with pytest.raises(ValueError, match="anonymous observer cannot dispatch"):
        alerts.main(state_path=tmp_path / "state.json", dispatch=True)
    assert not github.requests
    alerts.main(state_path=tmp_path / "state.json")
    assert github.requests
    assert all(request.get_method() == "GET" and not request.has_header("Authorization")
               for request in github.requests)
    with pytest.raises(ValueError, match="cannot write"):
        alerts.gh(alerts.DISPATCH_PATH, {"ref": "main"})


def test_token_permits_only_the_refresh_dispatch(monkeypatch):
    monkeypatch.setenv("TR_PRICE_REFRESH_PUBLIC_API", "1")
    monkeypatch.setattr(alerts, "_github_token", TOKEN)
    monkeypatch.setattr(alerts, "build_opener", lambda *args: pytest.fail("no request"))
    for path, data in (("issues/123", {"state": "closed"}), ("issues", {"title": "x"}),
                       (alerts.DISPATCH_PATH, {"ref": "evil"}),
                       ("actions/workflows/deploy.yml/dispatches", {"ref": "main"})):
        with pytest.raises(ValueError, match="cannot write"):
            alerts.gh(path, data)


def test_token_file_is_refused_outside_the_host_observer(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_REPOSITORY", alerts.REPOSITORY)
    monkeypatch.delenv("TR_PRICE_REFRESH_PUBLIC_API", raising=False)
    with pytest.raises(ValueError, match="only for the host observer"):
        alerts.main(state_path=tmp_path / "state.json", github_token=TOKEN)


def test_token_never_reaches_errors_logs_or_sentry(tmp_path, monkeypatch, host, capsys):
    github = FakeGitHub(dispatch_status=401)
    monkeypatch.setattr(alerts, "build_opener", github)
    alerts.main(state_path=tmp_path / "state.json", dispatch=True, github_token=TOKEN)
    events = [payload["fingerprint"] for kind, payload in host if kind == "event"]
    assert ["price-refresh", "refresh_dispatch_failed"] in events
    output = capsys.readouterr()
    assert "Price refresh dispatch failed (RuntimeError)" in output.err
    assert TOKEN not in output.out + output.err + json.dumps(host)
    class Refusing:
        def open(self, request, timeout):
            assert request.get_header("Authorization") == f"Bearer {TOKEN}"
            raise HTTPError(request.full_url, 401, "Bad credentials", {}, None)

    monkeypatch.setattr(alerts, "build_opener", lambda *args: Refusing())
    monkeypatch.setattr(alerts, "_github_token", TOKEN)
    with pytest.raises(RuntimeError) as caught:
        alerts.public_gh("actions/runs/1/attempts/1/jobs?per_page=100", None)
    chain = "".join(traceback.format_exception(caught.value))
    assert "/actions/runs/1/" not in str(caught.value)
    assert TOKEN not in chain and TOKEN not in repr(caught.value)


def test_cli_never_echoes_the_token(tmp_path):
    root = Path(__file__).resolve().parents[1]
    token_file = tmp_path / "github-token"
    env = {"PATH": os.environ.get("PATH", ""), "TR_PRICE_REFRESH_PUBLIC_API": "1",
           "GITHUB_REPOSITORY": "someone/else"}
    for content in (TOKEN, "ghp_" + TOKEN):
        token_file.write_text(content)
        result = subprocess.run(  # noqa: S603 - this repository's script
            [sys.executable, str(root / "scripts/price_refresh_alerts.py"), "--state-file",
             str(tmp_path / "state.json"), "--github-token-file", str(token_file),
             "--dispatch-stalled", "--no-heartbeat"],
            capture_output=True, text=True, env=env, timeout=30, check=False,
        )
        assert result.returncode == 1
        assert "Price refresh monitoring failed (ValueError)" in result.stderr
        assert TOKEN not in result.stdout + result.stderr
