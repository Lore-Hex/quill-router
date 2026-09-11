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
