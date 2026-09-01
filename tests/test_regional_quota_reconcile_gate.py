from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest

from trusted_router import regional_quota_reconcile_cli as worker
from trusted_router import regional_quota_reconcile_gate as gate
from trusted_router.gcs_singleflight import GCSLease


@dataclass
class _Singleflight:
    lease: GCSLease | None
    finish_error: Exception | None = None

    def __post_init__(self) -> None:
        self.finished: list[bool] = []

    def acquire(self) -> GCSLease | None:
        return self.lease

    def finish(self, _lease: GCSLease, *, succeeded: bool) -> float:
        self.finished.append(succeeded)
        if self.finish_error is not None:
            raise self.finish_error
        return 12.5


@pytest.fixture(autouse=True)
def _capture_gate_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=gate.__name__)


def test_overlap_exits_before_importing_worker(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    singleflight = _Singleflight(None)
    monkeypatch.setattr(gate, "_lease_from_environment", lambda: singleflight)
    monkeypatch.setattr(
        worker,
        "main",
        lambda: pytest.fail("overlap reached the database worker"),
    )

    assert gate.main() == 0
    assert singleflight.finished == []
    assert "regional_quota.reconciler_singleflight_skip" in caplog.text


@pytest.mark.parametrize("worker_status", [0, 1])
def test_leader_releases_lease_with_worker_outcome(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    worker_status: int,
) -> None:
    lease = GCSLease(owner="execution", generation=5, acquired_at=100.0)
    singleflight = _Singleflight(lease)
    monkeypatch.setattr(gate, "_lease_from_environment", lambda: singleflight)
    monkeypatch.setattr(worker, "main", lambda: worker_status)

    assert gate.main() == worker_status
    assert singleflight.finished == [worker_status == 0]
    assert "regional_quota.reconciler_singleflight_acquired" in caplog.text
    assert "regional_quota.reconciler_singleflight_released" in caplog.text


def test_admission_failure_fails_closed_before_worker(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail() -> _Singleflight:
        raise RuntimeError("GCS unavailable")

    monkeypatch.setattr(gate, "_lease_from_environment", fail)
    monkeypatch.setattr(
        worker,
        "main",
        lambda: pytest.fail("failed admission reached worker"),
    )

    assert gate.main() == 1
    assert "regional_quota.reconciler_singleflight_failed" in caplog.text


def test_release_failure_is_reported_as_failed_execution(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    lease = GCSLease(owner="execution", generation=5, acquired_at=100.0)
    singleflight = _Singleflight(lease, finish_error=RuntimeError("release lost"))
    monkeypatch.setattr(gate, "_lease_from_environment", lambda: singleflight)
    monkeypatch.setattr(worker, "main", lambda: 0)

    assert gate.main() == 1
    assert singleflight.finished == [True]
    assert "regional_quota.reconciler_singleflight_release_failed" in caplog.text


def test_lock_lease_must_outlive_cloud_run_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TR_REGIONAL_QUOTA_RECONCILER_LOCK_BUCKET", "bucket")
    monkeypatch.setenv("TR_REGIONAL_QUOTA_RECONCILER_LOCK_LEASE_SECONDS", "180")

    with pytest.raises(ValueError, match="must exceed"):
        gate._lease_from_environment()
