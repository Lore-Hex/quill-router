import json
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
    assert observations == [{"state_path": None}]


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
