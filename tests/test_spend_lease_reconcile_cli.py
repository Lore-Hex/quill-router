from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import trusted_router.spend_lease_reconcile_cli as cli

ROOT = Path(__file__).resolve().parents[1]


class _Store:
    def __init__(self) -> None:
        self.events: list[str] = []

    def verify_spend_lease_ledger(self) -> tuple[str, ...]:
        self.events.append("health")
        return ("us-central1",)

    def acquire_spend_lease_reconciler_lock(self, **_kwargs: Any) -> Any:
        self.events.append("acquire")
        return SimpleNamespace(fencing_token=7)

    def release_spend_lease_reconciler_lock(self, **_kwargs: Any) -> bool:
        self.events.append("release")
        return True

    def reconcile_spend_leases(self, **_kwargs: Any) -> dict[str, int]:
        self.events.append("reconcile")
        return {
            "candidates": 0,
            "open": 0,
            "recovered": 0,
            "bound": 0,
            "closed": 0,
            "deferred": 0,
            "errors": 0,
            "dead": 0,
        }

    def requeue_dead_spend_leases(self, **_kwargs: Any) -> int:
        self.events.append("requeue")
        return 2


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        spend_lease_reconciler_worker=True,
        spend_lease_reconcile_limit=25,
        spend_lease_reconcile_max_attempts=12,
    )


def _wire(monkeypatch: pytest.MonkeyPatch, store: _Store) -> list[str]:
    heartbeats: list[str] = []
    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "init_sentry", lambda _settings: None)
    monkeypatch.setattr(cli, "create_store", lambda _settings: store)
    monkeypatch.setattr(cli, "configure_store", lambda _store: None)
    monkeypatch.setattr(
        cli,
        "record_heartbeat",
        lambda target, *, settings: heartbeats.append(target),
    )
    return heartbeats


def test_clean_empty_pass_health_checks_before_work_and_heartbeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    heartbeats = _wire(monkeypatch, store)

    assert cli.main(["reconcile"]) == 0

    assert store.events == ["acquire", "health", "reconcile", "release"]
    assert heartbeats == ["job:spend-lease-reconcile"]


def test_failed_ledger_health_gate_does_no_work_or_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    heartbeats = _wire(monkeypatch, store)
    monkeypatch.setattr(store, "verify_spend_lease_ledger", lambda: ())

    assert cli.main(["reconcile"]) == 1

    assert store.events == ["acquire", "release"]
    assert heartbeats == []


def test_operator_requeue_runs_after_health_gate_without_worker_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    _wire(monkeypatch, store)

    assert cli.main(["requeue-dead", "lease-a", "lease-b"]) == 0

    assert store.events == ["health", "requeue"]


def test_spend_lease_reconciler_deploy_and_workflow_wiring() -> None:
    script = (ROOT / "scripts/deploy/spend_lease_reconciler.sh").read_text()
    orchestrator = (ROOT / "scripts/deploy-gcp.sh").read_text()
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()

    assert "trusted-router-spend-lease-reconciler" in script
    assert "--task-timeout 50s" in script
    assert "--max-retries 0" in script
    assert "--max-retry-attempts=0" in script
    assert '* * * * *' in script
    assert 'scheduler_state" = "PAUSED' in script
    assert "TR_SPEND_LEASE_BINDING_ENABLED" not in script
    assert "spend_lease_reconcile_cli,reconcile" in script
    assert "bash \"${SCRIPT_DIR}/deploy/spend_lease_reconciler.sh\"" in orchestrator
    assert "bash scripts/deploy/spend_lease_reconciler.sh" in workflow
