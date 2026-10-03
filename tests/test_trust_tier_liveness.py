from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from trusted_router import trust_owner_budget, trust_tier_cli

NOW = datetime(2026, 9, 11, 21, tzinfo=UTC)


class WorkerStopped(BaseException):
    """A platform deadline must not be swallowed as a workspace failure."""


class TierStore:
    def __init__(self, count: int = 1) -> None:
        self.calls: list[str] = []
        self.count = count

    def _owner_shard_counts_tx(self, *_args: Any) -> None:
        raise AssertionError("the owner scan is replaced by a test spy")

    def list_trust_tier_workspace_ids(self) -> tuple[str, ...]:
        self.calls.append("enumerate")
        return tuple(f"ws-{index}" for index in range(self.count))

    def recompute_workspace_trust_tier(self, workspace_id: str, **_kwargs: Any) -> int:
        self.calls.append(workspace_id)
        return 1


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        trust_qualifying_provider_set=frozenset({"stripe", "x402"}),
        trust_tier3_min_days=30,
        trust_tier3_min_paid_microdollars=50_000_000,
    )


def test_owner_evidence_refresh_precedes_a_killed_workspace_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InterruptedStore(TierStore):
        def recompute_workspace_trust_tier(self, workspace_id: str, **kwargs: Any) -> int:
            raise WorkerStopped()

    store = InterruptedStore()

    def refresh(actual_store: Any, **kwargs: Any) -> dict[str, Any]:
        assert actual_store is store
        assert kwargs == {"environment": "production", "now": NOW}
        store.calls.append("owner_budget")
        return {"scan_complete": True, "violating_owners": []}

    monkeypatch.setattr(trust_owner_budget, "recompute_owner_budget", refresh)
    with pytest.raises(WorkerStopped):
        trust_tier_cli.run(store, settings(), now=NOW)
    assert store.calls == ["owner_budget", "enumerate"]


def test_owner_scan_uses_its_own_start_clock_without_a_test_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Any] = []

    def refresh(_store: Any, **kwargs: Any) -> dict[str, Any]:
        observed.append(kwargs["now"])
        return {"scan_complete": True, "violating_owners": []}

    monkeypatch.setattr(trust_owner_budget, "recompute_owner_budget", refresh)
    result = trust_tier_cli.run(TierStore(), settings())
    assert observed == [None]
    assert result.succeeded == 1


@pytest.mark.parametrize("failure", ["scan", "violation", "persist"])
def test_owner_failure_does_not_skip_workspace_reconciliation(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    def refresh(_store: Any, **_kwargs: Any) -> dict[str, Any]:
        if failure == "persist":
            raise RuntimeError("persistence unavailable")
        return {
            "scan_complete": failure != "scan",
            "violating_owners": ["owner"] if failure == "violation" else [],
        }

    monkeypatch.setattr(trust_owner_budget, "recompute_owner_budget", refresh)
    store = TierStore(3)
    result = trust_tier_cli.run(store, settings(), now=NOW)
    assert result.owner_budget_failed
    assert result.succeeded == 3
    assert store.calls == ["enumerate", "ws-0", "ws-1", "ws-2"]


def test_large_sweep_reports_bounded_progress(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        trust_owner_budget, "recompute_owner_budget",
        lambda *_args, **_kwargs: {"scan_complete": True, "violating_owners": []},
    )
    caplog.set_level("INFO", logger="trusted_router.trust_tier_cli")
    result = trust_tier_cli.run(TierStore(1001), settings(), now=NOW)
    progress = [r.getMessage() for r in caplog.records if "trust.tier_job_progress" in r.message]
    assert result.succeeded == 1001
    assert len(progress) == 11
    assert "completed=100 total=1001 failed=0 elapsed_seconds=" in progress[0]
    assert "completed=1001 total=1001 failed=0 elapsed_seconds=" in progress[-1]


# --- the per-workspace pass runs in parallel ---


def concurrent_settings(concurrency: int) -> SimpleNamespace:
    values = settings()
    values.trust_tier_job_concurrency = concurrency
    return values


def no_owner_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        trust_owner_budget, "recompute_owner_budget",
        lambda *_args, **_kwargs: {"scan_complete": True, "violating_owners": []},
    )


def test_a_parallel_pass_visits_every_workspace_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    no_owner_scan(monkeypatch)
    caplog.set_level("INFO", logger="trusted_router.trust_tier_cli")
    store = TierStore(1001)

    result = trust_tier_cli.run(store, concurrent_settings(8), now=NOW)

    assert result.succeeded == 1001 and result.failed == ()
    visited = [call for call in store.calls if call != "enumerate"]
    assert sorted(visited) == sorted(f"ws-{index}" for index in range(1001))
    progress = [r.getMessage() for r in caplog.records if "trust.tier_job_progress" in r.message]
    assert len(progress) == 11
    assert "completed=1001 total=1001 failed=0" in progress[-1]


def test_a_parallel_pass_isolates_ordinary_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    no_owner_scan(monkeypatch)

    class FlakyStore(TierStore):
        def recompute_workspace_trust_tier(self, workspace_id: str, **kwargs: Any) -> int:
            if workspace_id in {"ws-3", "ws-70"}:
                raise RuntimeError("row diverged")
            return super().recompute_workspace_trust_tier(workspace_id, **kwargs)

    store = FlakyStore(100)
    result = trust_tier_cli.run(store, concurrent_settings(4), now=NOW)

    assert sorted(result.failed) == ["ws-3", "ws-70"]
    assert result.succeeded == 98
    assert len([call for call in store.calls if call != "enumerate"]) == 98


def test_a_platform_stop_in_a_worker_stops_the_parallel_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    no_owner_scan(monkeypatch)

    class StoppingStore(TierStore):
        def recompute_workspace_trust_tier(self, workspace_id: str, **kwargs: Any) -> int:
            if workspace_id == "ws-5":
                raise WorkerStopped()
            return super().recompute_workspace_trust_tier(workspace_id, **kwargs)

    store = StoppingStore(1000)
    with pytest.raises(WorkerStopped):
        trust_tier_cli.run(store, concurrent_settings(4), now=NOW)
    # The rest is cancelled, not run to completion.
    assert len([call for call in store.calls if call != "enumerate"]) < 999


def test_a_stop_is_seen_while_another_worker_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time

    no_owner_scan(monkeypatch)
    blocked = threading.Event()
    release = threading.Event()

    class BlockingStore(TierStore):
        def replicate_workspace_trust_reconciled_through(self, workspace_id: str, *_args: Any, **_kwargs: Any):
            if workspace_id == "ws-0":
                blocked.set()
                release.wait(10)
            return None

        def recompute_workspace_trust_tier(self, workspace_id: str, **kwargs: Any) -> int:
            if workspace_id == "ws-1":
                assert blocked.wait(5)
                raise WorkerStopped()
            return super().recompute_workspace_trust_tier(workspace_id, **kwargs)

    # Four workers, two workspaces: both are in flight when submission ends,
    # so the stop has to be found by the final drain.
    store = BlockingStore(2)
    started = time.monotonic()
    with pytest.raises(WorkerStopped):
        trust_tier_cli.run(store, concurrent_settings(4), now=NOW)
    # The stop surfaced while ws-0 was still blocked, not after it finished.
    assert time.monotonic() - started < 5
    assert not release.is_set()

    release.set()
    for thread in threading.enumerate():
        if thread.name.startswith("trust-tier"):
            thread.join(5)
    # Released after the stop, ws-0 wrote nothing.
    assert "ws-0" not in store.calls


def test_the_setting_defaults_to_one_worker_and_reads_its_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.config import Settings

    monkeypatch.delenv("TR_TRUST_TIER_JOB_CONCURRENCY", raising=False)
    assert Settings().trust_tier_job_concurrency == 1
    monkeypatch.setenv("TR_TRUST_TIER_JOB_CONCURRENCY", "8")
    assert Settings().trust_tier_job_concurrency == 8


def test_the_job_has_a_session_for_every_worker() -> None:
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "scripts/deploy/trust_tier_job.sh").read_text()
    pool = int(script.split('"TR_SPANNER_POOL_SIZE=', 1)[1].split('"', 1)[0])
    workers = int(script.split('"TR_TRUST_TIER_JOB_CONCURRENCY=', 1)[1].split('"', 1)[0])
    assert workers > 1
    assert pool >= workers
