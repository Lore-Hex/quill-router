from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trusted_router import config as config_module
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
        service_surface="control",
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


def test_deployed_job_env_selects_only_surface_compatible_with_its_bindings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Exercise the same zero-argument factory the Cloud Run Job CLI calls,
    # isolated from an operator shell, repo .env, or local developer key file.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(  # noqa: SLF001 - isolate the local-only Settings source.
        config_module._LocalKeyFileSource,
        "__call__",
        lambda _self: {},
    )
    for name in tuple(os.environ):
        if name.startswith("TR_"):
            monkeypatch.delenv(name)
    job_env = {
        "TR_ENVIRONMENT": "worker",
        "TR_STORAGE_BACKEND": "spanner-bigtable",
        "TR_GCP_PROJECT_ID": "project",
        "TR_SPANNER_INSTANCE_ID": "instance",
        "TR_SPANNER_DATABASE_ID": "database",
        "TR_BIGTABLE_INSTANCE_ID": "bigtable",
        "TR_BIGTABLE_GENERATION_TABLE": "generations",
        "TR_REQUEST_RECORD_WRITE_MODE": "typed",
        "TR_SETTLE_OUTBOX_ENABLED": "true",
        "TR_REGIONAL_QUOTA_LEASES_ENABLED": "true",
        "TR_REGIONAL_QUOTA_RECONCILER_WORKER": "true",
        "TR_REGIONAL_QUOTA_BIGTABLE_TABLE": "regional-quota",
        "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES": "us-central1=quota-us",
        "TR_REGIONAL_QUOTA_RECONCILE_LIMIT": "25",
        "TR_PRIMARY_REGION": "us-central1",
        "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED": "true",
        "TR_SENTRY_DSN": "https://example@example.ingest.sentry.io/1",
    }
    for name, value in job_env.items():
        monkeypatch.setenv(name, value)

    invalid_surfaces = ("combined", "public", "actions", "internal", "observer")
    for surface in invalid_surfaces:
        monkeypatch.setenv("TR_SERVICE_SURFACE", surface)
        with pytest.raises(ValidationError):
            worker.get_settings()

    monkeypatch.setenv("TR_SERVICE_SURFACE", "control")
    settings = worker.get_settings()

    assert settings.environment == "worker"
    assert settings.service_surface == "control"
    assert settings.regional_quota_reconciler_worker is True
    assert settings.sentry_dsn == job_env["TR_SENTRY_DSN"]


def test_generic_worker_issuance_still_requires_serving_pilot_allowlist() -> None:
    from pydantic import ValidationError

    from trusted_router.config import Settings

    with pytest.raises(ValidationError, match="PILOT_WORKSPACE_IDS"):
        Settings(
            environment="worker",
            service_surface="control",
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
