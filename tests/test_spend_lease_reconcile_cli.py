from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import trusted_router.spend_lease_reconcile_cli as cli
from trusted_router.spend_lease_ledger import SpendLeaseLedgerUnprovisioned

ROOT = Path(__file__).resolve().parents[1]


class _Store:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.health_error: Exception | None = None

    def verify_spend_lease_ledger(self) -> tuple[str, ...]:
        self.events.append("health")
        if self.health_error is not None:
            raise self.health_error
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


def test_unprovisioned_ledger_is_clean_idle_pass_with_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _Store()
    store.health_error = SpendLeaseLedgerUnprovisioned(
        table_id="trustedrouter-spend-lease",
        profile="tr-spend-us-central1",
        region="us-central1",
    )
    heartbeats = _wire(monkeypatch, store)

    with caplog.at_level(logging.INFO, logger=cli.__name__):
        assert cli.main(["reconcile"]) == 0

    assert store.events == ["acquire", "health", "release"]
    assert heartbeats == ["job:spend-lease-reconcile"]
    assert caplog.messages.count(
        "spend_lease.reconciler_ledger_unprovisioned "
        "table=trustedrouter-spend-lease "
        "profile=tr-spend-us-central1 region=us-central1"
    ) == 1


def test_generic_health_failure_returns_one_without_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    store.health_error = RuntimeError("ledger transport failed")
    heartbeats = _wire(monkeypatch, store)

    assert cli.main(["reconcile"]) == 1

    assert store.events == ["acquire", "health", "release"]
    assert heartbeats == []


def test_operator_requeue_runs_after_health_gate_without_worker_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    _wire(monkeypatch, store)

    assert cli.main(["requeue-dead", "lease-a", "lease-b"]) == 0

    assert store.events == ["health", "requeue"]


def test_operator_requeue_rejects_unprovisioned_ledger(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _Store()
    store.health_error = SpendLeaseLedgerUnprovisioned(
        table_id="trustedrouter-spend-lease",
        profile="tr-spend-us-central1",
        region="us-central1",
    )
    heartbeats = _wire(monkeypatch, store)

    with caplog.at_level(logging.ERROR, logger=cli.__name__):
        assert cli.main(["requeue-dead"]) == 1

    assert store.events == ["health"]
    assert heartbeats == []
    assert (
        "spend_lease.requeue_ledger_unprovisioned "
        "table=trustedrouter-spend-lease profile=tr-spend-us-central1 "
        "region=us-central1 nothing_to_requeue=true"
    ) in caplog.messages


def test_spend_lease_reconciler_deploy_and_workflow_wiring() -> None:
    script = (ROOT / "scripts/deploy/spend_lease_reconciler.sh").read_text()
    orchestrator = (ROOT / "scripts/deploy-gcp.sh").read_text()
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
    rollout_secondaries = workflow.split("\n  rollout-secondaries:\n", 1)[1].split(
        "\n  public-surface-companion:\n", 1
    )[0]
    ramp_step = rollout_secondaries.split(
        "- name: Ramp secondaries serially while reconciler deploys", 1
    )[1].split("- name: Deploy synthetic monitor", 1)[0]
    ramp_script = (ROOT / "scripts/deploy/ramp_secondaries.sh").read_text()

    assert "trusted-router-spend-lease-reconciler" in script
    assert "--task-timeout 50s" in script
    assert "--max-retries 0" in script
    assert "--max-retry-attempts=0" in script
    assert '* * * * *' in script
    assert 'scheduler_state" = "PAUSED' in script
    assert "TR_SPEND_LEASE_BINDING_ENABLED" not in script
    assert "spend_lease_reconcile_cli,reconcile" in script
    assert "bash \"${SCRIPT_DIR}/deploy/spend_lease_reconciler.sh\"" in orchestrator
    assert "run: bash scripts/deploy/ramp_secondaries.sh" in ramp_step

    regional_launch = ramp_script.index(
        'bash "${SCRIPT_DIR}/regional_quota_reconciler.sh" '
        '>"${reconciler_log}" 2>&1 &'
    )
    spend_lease_launch = ramp_script.index(
        'bash "${SCRIPT_DIR}/spend_lease_reconciler.sh" '
        '>"${spend_lease_reconciler_log}" 2>&1 &'
    )
    regional_pid = ramp_script.index("reconciler_pid=$!", regional_launch)
    spend_lease_pid = ramp_script.index(
        "spend_lease_reconciler_pid=$!", spend_lease_launch
    )
    ramps = ramp_script.index("for region in europe-west4 us-east4 southamerica-east1")
    regional_wait = ramp_script.index('if wait "${reconciler_pid}"; then')
    spend_lease_wait = ramp_script.index(
        'if wait "${spend_lease_reconciler_pid}"; then'
    )
    regional_log = ramp_script.index('cat "${reconciler_log}"')
    spend_lease_log = ramp_script.index('cat "${spend_lease_reconciler_log}"')
    ramp_status = ramp_script.index('if [ "${ramp_status}" -ne 0 ]; then')
    regional_status = ramp_script.index('if [ "${reconciler_status}" -ne 0 ]; then')
    spend_lease_status = ramp_script.index(
        'if [ "${spend_lease_reconciler_status}" -ne 0 ]; then'
    )
    assert regional_launch < regional_pid < ramps < regional_wait < regional_log
    assert spend_lease_launch < spend_lease_pid < ramps < spend_lease_wait < spend_lease_log
    assert regional_log < ramp_status
    assert spend_lease_log < ramp_status
    assert ramp_status < regional_status < spend_lease_status
