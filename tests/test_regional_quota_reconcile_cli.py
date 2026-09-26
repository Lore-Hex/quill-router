from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trusted_router import config as config_module
from trusted_router import regional_quota_reconcile_cli as worker


@pytest.fixture(autouse=True)
def _isolate_observability(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(worker, "init_sentry", lambda _settings: None)
    monkeypatch.setattr(worker, "record_heartbeat", lambda *_args, **_kwargs: None)
    caplog.set_level(logging.INFO, logger=worker.__name__)


class _Store:
    def __init__(
        self,
        result: dict[str, int],
        *,
        lock_available: bool = True,
        release_result: bool = True,
        reconcile_error: Exception | None = None,
        release_error: Exception | None = None,
        previous_owner: str | None = None,
        previous_fencing_token: int | None = None,
    ) -> None:
        self.result = result
        self.limits: list[int] = []
        self.lock_available = lock_available
        self.release_result = release_result
        self.reconcile_error = reconcile_error
        self.release_error = release_error
        self.previous_owner = previous_owner
        self.previous_fencing_token = previous_fencing_token
        self.released: list[tuple[str, int]] = []

    def reconcile_regional_quota_leases(self, *, limit: int, max_seconds: float) -> dict[str, int]:
        assert 0 <= max_seconds <= 45
        self.limits.append(limit)
        if self.reconcile_error is not None:
            raise self.reconcile_error
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
        return SimpleNamespace(
            fencing_token=7,
            previous_owner=self.previous_owner,
            previous_fencing_token=self.previous_fencing_token,
        )

    def release_regional_quota_reconciler_lock(
        self,
        *,
        owner: str,
        fencing_token: int,
    ) -> bool:
        self.released.append((owner, fencing_token))
        if self.release_error is not None:
            raise self.release_error
        return self.release_result


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


def test_worker_reconciles_with_bounded_limit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _Store({"inspected": 2, "reconciled": 1, "closed": 1, "errors": 0,
                    "backlog": 5, "processed": 2, "remaining": 3,
                    "completed": 1, "abandoned": 1, "budget_exhausted": 1})
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
    assert "backlog=5 processed=2 remaining=3" in caplog.text
    assert len(store.released) == 1
    assert store.released[0][1] == 7
    assert heartbeats == ["job:regional-quota-reconcile"]
    assert "regional_quota.reconciler_start" in caplog.text
    assert "regional_quota.reconciler_settings_loaded" in caplog.text
    assert "regional_quota.reconciler_store_open_start" in caplog.text
    assert "regional_quota.reconciler_store_open_complete" in caplog.text
    assert "regional_quota.reconciler_lock_acquire_start" in caplog.text
    assert "regional_quota.reconciler_lock_acquired" in caplog.text
    assert "regional_quota.reconciler_lock_released" in caplog.text
    assert "regional_quota.reconciler_complete" in caplog.text


def test_worker_logs_expired_lock_takeover(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _Store(
        {"inspected": 1, "reconciled": 1, "closed": 1, "errors": 0},
        previous_owner="rqrec-expired-owner",
        previous_fencing_token=6,
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
    assert "regional_quota.reconciler_lock_takeover" in caplog.text
    assert "previous_owner=rqrec-expired-owner" in caplog.text
    assert "previous_fencing_token=6" in caplog.text


def test_reconcile_exception_still_releases_lock_and_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _Store(
        {"inspected": 0, "reconciled": 0, "closed": 0, "errors": 0},
        reconcile_error=RuntimeError("reconcile exploded"),
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

    assert worker.main() == 1
    assert len(store.released) == 1
    assert "regional_quota.reconciler_failed" in caplog.text
    assert "regional_quota.reconciler_lock_released" in caplog.text


def test_release_lost_exits_one_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _Store(
        {"inspected": 1, "reconciled": 1, "closed": 1, "errors": 0},
        release_result=False,
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

    assert worker.main() == 1
    assert "regional_quota.reconciler_lock_release_lost" in caplog.text
    assert "regional_quota.reconciler_lock_released" not in caplog.text


def test_release_exception_is_caught_and_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _Store(
        {"inspected": 1, "reconciled": 1, "closed": 1, "errors": 0},
        release_error=RuntimeError("release exploded"),
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

    assert worker.main() == 1
    assert "regional_quota.reconciler_lock_release_failed" in caplog.text
    assert "regional_quota.reconciler_lock_release_lost" not in caplog.text


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
    caplog: pytest.LogCaptureFixture,
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
    assert "regional_quota.reconciler_lock_busy" in caplog.text


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


def test_deployed_job_env_rejects_surfaces_that_do_not_own_its_bindings(
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

    invalid_surfaces = ("combined", "actions", "internal", "observer")
    for surface in invalid_surfaces:
        monkeypatch.setenv("TR_SERVICE_SURFACE", surface)
        with pytest.raises(ValidationError):
            worker.get_settings()

    # The newer T1 error-reporting contract makes Sentry public-owned too, so
    # this binding can no longer serve as proof that the worker env is invalid
    # for public. The public ownership and its much narrower deployed secret
    # allowlist are asserted in test_service_surface_secret_isolation.py and
    # test_public_surface_deploy.py respectively. The real reconciler remains
    # explicitly control below and in regional_quota_reconciler.sh.
    monkeypatch.setenv("TR_SERVICE_SURFACE", "public")
    public_compatible = worker.get_settings()
    assert public_compatible.sentry_dsn == job_env["TR_SENTRY_DSN"]

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


@pytest.mark.parametrize("has_hold,discovery_seconds,slow_read,qualifying", [
    (True, 46.0, None, False), (True, 0.0, None, True), (False, 46.0, None, True),
    (True, 44.0, "global", False), (True, 44.0, "local", False),
])
def test_real_backend_completion_evidence_requires_progress_or_empty_backlog(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path,
    has_hold: bool, discovery_seconds: float, slow_read: str | None, qualifying: bool,
) -> None:
    from typing import Any

    from tests.test_regional_quota_activation import _activation_body, _replies, _run
    from tests.test_regional_quota_ledger import _FakeBigtableTable
    from tests.test_regional_quota_r2b import authorize, setup
    from trusted_router.regional_quota_ledger import BigtableRegionalQuotaLedger

    store, _db, _key, args = setup()
    store._regional_quota_ledger = BigtableRegionalQuotaLedger({"us-central1": _FakeBigtableTable()})
    if has_hold:
        authorize(store, args, "budget-hold")
    clock = [0.0]
    list_entities = type(store)._list_entities
    read_entity = type(store)._read_entity
    ledger_get = BigtableRegionalQuotaLedger.get
    results = []
    reconcile = type(store).reconcile_regional_quota_leases

    def slow_discovery(self: Any, kind: str, **kwargs: Any) -> Any:
        entities = list_entities(self, kind, **kwargs)
        if kind == "regional_quota_lease_open":
            clock[0] += discovery_seconds
        return entities

    def slow_global_read(self: Any, kind: str, *args: Any, **kwargs: Any) -> Any:
        entity = read_entity(self, kind, *args, **kwargs)
        if kind == "regional_quota_lease" and slow_read == "global":
            clock[0] += 2.0
        return entity

    def slow_local_read(self: Any, *args: Any, **kwargs: Any) -> Any:
        entity = ledger_get(self, *args, **kwargs)
        if slow_read == "local":
            clock[0] += 2.0
        return entity

    def capture_reconcile(self: Any, **kwargs: Any) -> Any:
        result = reconcile(self, **kwargs)
        results.append(result)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(worker.time, "monotonic", lambda: clock[0])
        patch.setattr(type(store), "_list_entities", slow_discovery)
        patch.setattr(type(store), "_read_entity", slow_global_read)
        patch.setattr(BigtableRegionalQuotaLedger, "get", slow_local_read)
        patch.setattr(type(store), "reconcile_regional_quota_leases", capture_reconcile)
        patch.setattr(worker, "get_settings", lambda: SimpleNamespace(
            regional_quota_reconciler_worker=True, regional_quota_leases_enabled=True,
            regional_quota_reconcile_limit=100,
        ))
        patch.setattr(worker, "create_store", lambda _settings: store)
        patch.setattr(worker, "configure_store", lambda _store: None)
        # Repeated invocations must not manufacture fresh completion evidence
        # while the same durable hold never reaches reconciliation.
        for _ in range(2):
            assert worker.main() == 0
    assert len(results) == 2
    emitted = [record.getMessage() for record in caplog.records if
               record.getMessage().startswith(("regional_quota.reconciler_complete elapsed_ms=",
                                               "regional_quota.reconciler_budget_exhausted elapsed_ms="))]
    assert len(emitted) == 2
    assert emitted[0] == emitted[1]
    replies = _replies()
    replies["evidence"] = [{"textPayload": emitted[0]}]
    # Apply the actual textPayload filter to real emitted evidence, like Logging.
    body = r'''eval "$(declare -f gc | sed '1s/gc/recorded_gc/')"
    export -f recorded_gc
    gc() {
      if [ "$1 $2" = "logging read" ]; then
        echo "$*" >> "$FIXTURES/calls"
        python3 -c '
import json, re, sys
needle = re.search(r"textPayload:\"([^\"]+)\"", sys.argv[1]).group(1)
print(json.dumps([e for e in json.load(sys.stdin) if needle in e["textPayload"]]))
' "$3" < "$FIXTURES/evidence.json"
      else recorded_gc "$@"; fi
    }
''' + _activation_body()
    run = _run(tmp_path, body, replies=replies)
    assert (run.returncode == 0) is qualifying, run.stderr
    assert ("regional_quota.reconciler_complete elapsed_ms=" in emitted[0]) is qualifying
    assert ("regional_quota.reconciler_budget_exhausted elapsed_ms=" in emitted[0]) is not qualifying
    assert "textPayload:\"regional_quota.reconciler_complete elapsed_ms=\"" in (tmp_path / "calls").read_text()
    for result in results:
        assert result["errors"] == 0
        assert result["backlog"] == int(has_hold)
        assert result["processed"] == result["inspected"] == int(has_hold and (qualifying or slow_read is not None))
        assert result["remaining"] == int(has_hold and not qualifying and slow_read is None)
        assert result["completed"] == result["reconciled"] == int(has_hold and qualifying)
        assert result["abandoned"] == int(slow_read is not None)
        assert result["budget_exhausted"] == int(has_hold and not qualifying)
