from __future__ import annotations

from types import SimpleNamespace

import pytest

from trusted_router import regional_quota_reconcile_cli as worker


@pytest.fixture(autouse=True)
def _isolate_observability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "init_sentry", lambda _settings: None)
    monkeypatch.setattr(worker, "record_heartbeat", lambda *_args, **_kwargs: None)


class _Store:
    def __init__(self, result: dict[str, int], *, lock_available: bool = True) -> None:
        self.result = result
        self.limits: list[int] = []
        self.lock_available = lock_available
        self.released: list[tuple[str, int]] = []

    def reconcile_regional_quota_leases(self, *, limit: int) -> dict[str, int]:
        self.limits.append(limit)
        return self.result

    def verify_regional_quota_ledger(self) -> tuple[str, ...]:
        return ("us-central1",)

    def acquire_regional_quota_reconciler_lock(
        self,
        *,
        owner: str,
        ttl_seconds: int,
    ) -> SimpleNamespace | None:
        assert owner.startswith("rqrec-")
        assert ttl_seconds == 90
        if not self.lock_available:
            return None
        return SimpleNamespace(fencing_token=7)

    def release_regional_quota_reconciler_lock(
        self,
        *,
        owner: str,
        fencing_token: int,
    ) -> bool:
        self.released.append((owner, fencing_token))
        return True


def test_disabled_worker_does_not_open_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(regional_quota_reconciler_worker=False),
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
        lambda: SimpleNamespace(
            regional_quota_reconciler_worker=True,
            regional_quota_leases_enabled=True,
            regional_quota_reconcile_limit=1_000,
        ),
    )
    heartbeats: list[str] = []
    monkeypatch.setattr(worker, "create_store", lambda _settings: store)
    monkeypatch.setattr(
        worker,
        "record_heartbeat",
        lambda name, **_kwargs: heartbeats.append(name),
    )
    assert worker.main() == 0
    assert store.limits == [1000]
    assert len(store.released) == 1
    assert store.released[0][1] == 7
    assert heartbeats == ["job:regional-quota-reconcile"]


def test_worker_fails_execution_when_any_lease_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store({"inspected": 1, "reconciled": 0, "closed": 0, "errors": 1})
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(
            regional_quota_reconciler_worker=True,
            regional_quota_leases_enabled=True,
            regional_quota_reconcile_limit=25,
        ),
    )
    monkeypatch.setattr(worker, "create_store", lambda _settings: store)

    assert worker.main() == 1
    assert len(store.released) == 1


def test_worker_fails_closed_without_reconcile_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(
            regional_quota_reconciler_worker=True,
            regional_quota_leases_enabled=True,
            regional_quota_reconcile_limit=25,
        ),
    )
    monkeypatch.setattr(
        worker,
        "create_store",
        lambda _settings: SimpleNamespace(
            verify_regional_quota_ledger=lambda: ("us-central1",),
            acquire_regional_quota_reconciler_lock=lambda **_kwargs: SimpleNamespace(
                fencing_token=1
            ),
            release_regional_quota_reconciler_lock=lambda **_kwargs: True,
        ),
    )

    assert worker.main() == 1


def test_worker_fails_closed_when_no_ledger_region_is_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(
            regional_quota_reconciler_worker=True,
            regional_quota_leases_enabled=True,
            regional_quota_reconcile_limit=25,
        ),
    )
    monkeypatch.setattr(
        worker,
        "create_store",
        lambda _settings: SimpleNamespace(
            verify_regional_quota_ledger=lambda: (),
            reconcile_regional_quota_leases=lambda **_kwargs: {},
            acquire_regional_quota_reconciler_lock=lambda **_kwargs: SimpleNamespace(
                fencing_token=1
            ),
            release_regional_quota_reconciler_lock=lambda **_kwargs: True,
        ),
    )

    assert worker.main() == 1


def test_worker_skips_when_another_reconciler_owns_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(
        {"inspected": 1, "reconciled": 1, "closed": 1, "errors": 0},
        lock_available=False,
    )
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(
            regional_quota_reconciler_worker=True,
            regional_quota_leases_enabled=True,
            regional_quota_reconcile_limit=25,
        ),
    )
    monkeypatch.setattr(worker, "create_store", lambda _settings: store)

    assert worker.main() == 0
    assert store.limits == []
    assert store.released == []


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
        regional_quota_reconciler_worker=True,
        regional_quota_bigtable_app_profiles="us-central1=quota-us",
    )

    assert settings.regional_quota_lease_pilot_workspaces == frozenset()


def test_generic_worker_issuance_still_requires_serving_pilot_allowlist() -> None:
    from pydantic import ValidationError

    from trusted_router.config import Settings

    with pytest.raises(ValidationError, match="PILOT_WORKSPACE_IDS"):
        Settings(
            environment="worker",
            storage_backend="spanner-bigtable",
            gcp_project_id="project",
            spanner_instance_id="instance",
            spanner_database_id="database",
            bigtable_instance_id="bigtable",
            request_record_write_mode="typed",
            settle_outbox_enabled=True,
            regional_quota_leases_enabled=True,
            regional_quota_lease_issuance_enabled=True,
            regional_quota_bigtable_app_profiles="us-central1=quota-us",
        )
