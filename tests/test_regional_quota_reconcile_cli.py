from __future__ import annotations

from types import SimpleNamespace

import pytest

from trusted_router import regional_quota_reconcile_cli as worker


class _Store:
    def __init__(self, result: dict[str, int]) -> None:
        self.result = result
        self.limits: list[int] = []

    def reconcile_regional_quota_leases(self, *, limit: int) -> dict[str, int]:
        self.limits.append(limit)
        return self.result


def test_disabled_worker_does_not_open_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(regional_quota_leases_enabled=False),
    )
    monkeypatch.setattr(
        worker,
        "create_store",
        lambda _settings: pytest.fail("disabled worker opened storage"),
    )

    assert worker.main() == 0


def test_worker_reconciles_with_bounded_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store({"inspected": 2, "reconciled": 2, "closed": 1, "errors": 0})
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(regional_quota_leases_enabled=True),
    )
    monkeypatch.setattr(worker, "create_store", lambda _settings: store)
    monkeypatch.setenv("TR_REGIONAL_QUOTA_RECONCILE_LIMIT", "5000")

    assert worker.main() == 0
    assert store.limits == [1000]


def test_worker_fails_execution_when_any_lease_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store({"inspected": 1, "reconciled": 0, "closed": 0, "errors": 1})
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(regional_quota_leases_enabled=True),
    )
    monkeypatch.setattr(worker, "create_store", lambda _settings: store)

    assert worker.main() == 1


def test_worker_rejects_non_integer_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store({"inspected": 0, "reconciled": 0, "closed": 0, "errors": 0})
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(regional_quota_leases_enabled=True),
    )
    monkeypatch.setattr(worker, "create_store", lambda _settings: store)
    monkeypatch.setenv("TR_REGIONAL_QUOTA_RECONCILE_LIMIT", "many")

    with pytest.raises(ValueError, match="must be an integer"):
        worker.main()


def test_worker_fails_closed_without_reconcile_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(regional_quota_leases_enabled=True),
    )
    monkeypatch.setattr(worker, "create_store", lambda _settings: SimpleNamespace())

    assert worker.main() == 1


def test_worker_environment_does_not_require_serving_pilot_allowlist() -> None:
    from trusted_router.config import Settings

    settings = Settings(
        environment="worker",
        storage_backend="spanner-bigtable",
        gcp_project_id="project",
        spanner_instance_id="instance",
        spanner_database_id="database",
        bigtable_instance_id="bigtable",
        request_record_write_mode="typed",
        settle_outbox_enabled=True,
        regional_quota_leases_enabled=True,
        regional_quota_bigtable_app_profiles="us-central1=quota-us",
    )

    assert settings.regional_quota_lease_pilot_workspaces == frozenset()
