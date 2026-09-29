"""A trust read cut short by the caller's RPC budget is not a trust-gate failure.

Admission runs the trust read last inside a shared Spanner budget. When earlier
reads spend that budget, the trust read times out; the request still falls back
to central authorize, but that must not page as a trust-gate read failure or
refuse every other caller for the verdict's cache lifetime.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from google.api_core.exceptions import DeadlineExceeded

from tests.fakes.spanner import make_fake_store
from tests.test_trust_eligibility_pr2 import arm_store, workspace_state
from trusted_router import storage_gcp_io
from trusted_router import trust_eligibility as gate


@pytest.fixture
def armed() -> tuple[Any, Any, Any, datetime]:
    store, db = make_fake_store(request_record_write_mode="typed")
    settings = arm_store(store, db)
    workspace_state(db)
    # Every gate call in a test evaluates and consumes evidence at this one
    # instant, taken after the evidence was written.
    return store, db, settings, datetime.now(UTC)


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    from trusted_router.synthetic import alerts as alerts_module

    monkeypatch.setattr(alerts_module, "ops_alert", lambda message, **_kw: sent.append(message))
    return sent


# Every test runs under a frozen monotonic clock, so no pause can expire a
# verdict or a deadline between two statements.
@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [100.0]
    monkeypatch.setattr(gate.time, "monotonic", lambda: now[0])
    return now


@contextmanager
def _shared_deadline(offset_seconds: float) -> Iterator[None]:
    token = storage_gcp_io._SPANNER_RPC_DEADLINE.set(time.monotonic() + offset_seconds)
    try:
        yield
    finally:
        storage_gcp_io._SPANNER_RPC_DEADLINE.reset(token)


def _deadline_exceeded(*_args: Any, **_kwargs: Any) -> Any:
    raise DeadlineExceeded("injected Spanner deadline")


def test_shared_rpc_budget_spent_tracks_the_callers_deadline() -> None:
    assert not storage_gcp_io.shared_rpc_budget_spent()  # no budget in scope
    with _shared_deadline(60):
        assert not storage_gcp_io.shared_rpc_budget_spent()
    with _shared_deadline(-0.001):
        assert storage_gcp_io.shared_rpc_budget_spent()


@pytest.mark.parametrize(("offset", "pages"), [(-1.0, False), (60.0, True)], ids=["spent", "remaining"])
def test_workspace_read_failure_pages_only_when_budget_remains(
    armed: Any, alerts: list[str], monkeypatch: pytest.MonkeyPatch, caplog: Any,
    offset: float, pages: bool,
) -> None:
    store, _db, settings, now = armed
    verdict = gate.global_trust_verdict(store, settings, now=now)
    assert verdict.failure is None
    monkeypatch.setattr(type(store), "_read_entity_tx", _deadline_exceeded)

    with _shared_deadline(offset):
        outcome = gate.lease_eligibility(
            store, settings, "workspace", global_verdict=verdict, now=now
        )

    # The request outcome is identical either way: no lease, central fallback.
    assert outcome == (None, "trust_gate_unarmed")
    records = [r for r in caplog.records if r.name == gate.log.name]
    if pages:
        assert alerts == ["trust.gate_unarmed condition=read_failed"]
        [failed] = [r for r in records if r.getMessage() == "trust.gate_unarmed read_failed"]
        assert failed.exc_info is not None
        assert isinstance(failed.exc_info[1], DeadlineExceeded) and failed.exc_info[2] is not None
    else:
        assert alerts == []
        assert [(r.levelno, r.getMessage()) for r in records] == [
            (logging.WARNING, f"trust.gate_unarmed {gate.ADMISSION_BUDGET_SPENT}")
        ]


def test_global_refresh_cut_short_by_budget_is_not_cached(
    armed: Any, alerts: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, settings, now = armed
    snapshot = type(db).snapshot
    monkeypatch.setattr(type(db), "snapshot", _deadline_exceeded)

    with _shared_deadline(-1.0):
        spent = gate.global_trust_verdict(store, settings, now=now)
        assert spent.failure == gate.ADMISSION_BUDGET_SPENT
        assert gate.lease_eligibility(
            store, settings, "workspace", global_verdict=spent, now=now
        ) == (None, "trust_gate_unarmed")
    assert alerts == []
    assert gate._caches[store].verdict is None

    # The next caller, with budget left, re-reads the evidence instead of
    # inheriting a refusal for the verdict's whole cache lifetime.
    monkeypatch.setattr(type(db), "snapshot", snapshot)
    fresh = gate.global_trust_verdict(store, settings, now=now)
    assert fresh.failure is None
    assert gate._caches[store].verdict is fresh


def test_global_refresh_failure_with_budget_left_is_still_cached_and_paged(
    armed: Any, alerts: list[str], monkeypatch: pytest.MonkeyPatch, clock: list[float],
) -> None:
    store, db, settings, now = armed
    snapshot = type(db).snapshot
    monkeypatch.setattr(type(db), "snapshot", _deadline_exceeded)

    with _shared_deadline(60.0):
        failed = gate.global_trust_verdict(store, settings, now=now)
        assert failed.failure == "read_failed"
        assert gate._caches[store].verdict is failed
        assert gate.lease_eligibility(
            store, settings, "workspace", global_verdict=failed, now=now
        ) == (None, "trust_gate_unarmed")
    assert alerts == ["trust.gate_unarmed condition=read_failed"]

    # A real read failure keeps its full negative-cache lifetime: until expiry
    # the next caller is refused from the cache without another read, even once
    # reads recover; at expiry the next caller reads again.
    monkeypatch.setattr(type(db), "snapshot", snapshot)
    clock[0] = failed.expires_monotonic - 0.001
    reads = (len(db.snapshot_calls), db.snapshot_execute_sql_calls)
    assert gate.global_trust_verdict(store, settings, now=now) is failed
    assert (len(db.snapshot_calls), db.snapshot_execute_sql_calls) == reads
    clock[0] = failed.expires_monotonic
    assert gate.global_trust_verdict(store, settings, now=now).failure is None
    assert len(db.snapshot_calls) == reads[0] + 1


def test_budget_cut_refresh_neither_replaces_nor_renews_an_older_verdict(
    armed: Any, alerts: list[str], monkeypatch: pytest.MonkeyPatch, clock: list[float],
) -> None:
    store, db, settings, now = armed
    primed = gate.global_trust_verdict(store, settings, now=now)
    assert primed.failure is None
    # Still valid but inside its refresh margin, so the next caller refreshes.
    clock[0] = primed.expires_monotonic - gate.GLOBAL_TRUST_REFRESH_MARGIN_SECONDS / 2
    snapshot = type(db).snapshot
    monkeypatch.setattr(type(db), "snapshot", _deadline_exceeded)

    with _shared_deadline(-1.0):
        spent = gate.global_trust_verdict(store, settings, now=now)
    assert spent.failure == gate.ADMISSION_BUDGET_SPENT
    # The older verdict is untouched, so its own expiry still bounds it.
    assert gate._caches[store].verdict is primed

    monkeypatch.setattr(type(db), "snapshot", snapshot)
    reads = len(db.snapshot_calls)
    fresh = gate.global_trust_verdict(store, settings, now=now)
    assert len(db.snapshot_calls) == reads + 1  # a real refresh, not a copy
    assert fresh.failure is None and fresh is not primed
    assert fresh.expires_monotonic == clock[0] + gate.GLOBAL_TRUST_TTL_SECONDS
    assert gate._caches[store].verdict is fresh
    assert alerts == []
