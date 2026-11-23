"""The workspace gate on the native Spanner store: same money, one at a time.

tests/test_workspace_billing_gate_store.py drives the route handlers over the
Spanner fake. This runs the store's own typed reserve and finalize on every
backend that is the real ``SpannerStore``, including the native emulator, so
the gated call sites execute the production GoogleSQL through the real SDK
transaction runner.

What the emulator can and cannot show: it admits one read-write transaction
at a time and aborts the others, which the SDK then redoes. So it shows that
a workspace's concurrent credit-row transactions from one process are redone
without the gate and are not with it, and that the balances come out the
same. It has no lock queue and no shared-then-exclusive upgrade, so it says
nothing about seconds of lock wait or about several processes.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from trusted_router import workspace_billing_gate
from trusted_router.config import Settings
from trusted_router.storage_gcp_io import spanner_rpc_budget
from trusted_router.store_protocol import Store
from trusted_router.workspace_billing_gate import FifoKeyedGate

CREDIT_ROW_TAGS = frozenset({"tr_authorize", "tr_finalize"})
CLIENTS = 4
ROUNDS = 2
HOLD = 1_000
CHARGE = 7
MODEL = "anthropic/claude-opus-4.7"


class Traffic:
    """Counts the credit-row transactions' callback runs: one per attempt."""

    def __init__(self, database: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.attempts: Counter[str] = Counter()
        self.finished: Counter[str] = Counter()
        self.inside = 0
        self.peak = 0
        self._lock = threading.Lock()
        original = database.run_in_transaction
        traffic = self

        def run(func: Any, *args: Any, **kwargs: Any) -> Any:
            tag = kwargs.get("transaction_tag")
            if tag not in CREDIT_ROW_TAGS:
                return original(func, *args, **kwargs)

            def counted(transaction: Any, *inner: Any, **named: Any) -> Any:
                with traffic._lock:
                    traffic.attempts[tag] += 1
                return func(transaction, *inner, **named)

            with traffic._lock:
                traffic.inside += 1
                traffic.peak = max(traffic.peak, traffic.inside)
            try:
                return original(counted, *args, **kwargs)
            finally:
                with traffic._lock:
                    traffic.inside -= 1
                    traffic.finished[tag] += 1

        monkeypatch.setattr(database, "run_in_transaction", run)

    @property
    def redone(self) -> int:
        return sum(self.attempts.values()) - sum(self.finished.values())


@spanner_rpc_budget(20.0)
def _request(store: Any, workspace_id: str, key_hash: str) -> None:
    """One request as the route runs it: a reserve, then the finalize, each
    inside the route's Spanner budget."""
    outcome, authorization = store.authorize_gateway_typed(
        workspace_id=workspace_id,
        key_hash=key_hash,
        estimate=HOLD,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id=MODEL,
        provider="anthropic",
        requested_model_id=MODEL,
        candidate_model_ids=[MODEL],
        region="us",
        endpoint_id=f"{MODEL}@anthropic/prepaid",
        candidate_endpoint_ids=[f"{MODEL}@anthropic/prepaid"],
        idempotency_key=None,
        idempotency_fingerprint=None,
        expires_at=dt.datetime(2099, 1, 1, tzinfo=dt.UTC),
    )
    assert authorization is not None, outcome
    result = store.typed_finalize_gateway_authorization_result(
        authorization.id, success=True, actual_microdollars=CHARGE,
        selected_usage_type="Credits",
    )
    assert result.finalized is True


@pytest.mark.parametrize("gate_on", [False, True], ids=["gate_off", "gate_on"])
def test_a_gated_burst_books_the_same_money_and_redoes_nothing(
    store: Store,
    workspace_id: str,
    unique: str,
    monkeypatch: pytest.MonkeyPatch,
    gate_on: bool,
) -> None:
    if not hasattr(store, "_credit_row_gate"):
        pytest.skip("this backend has no credit-row gate (not the native Spanner store)")
    native: Any = store
    gate = FifoKeyedGate()
    monkeypatch.setattr(workspace_billing_gate, "WORKSPACE_GATE", gate)
    monkeypatch.setattr(native, "trust_settings", Settings(
        environment="test",
        workspace_billing_gate_width=1 if gate_on else 0,
        # Nothing here should go around the gate by time.
        workspace_billing_gate_wait_ms=5000,
    ))
    funded = 10_000_000
    assert native.credit_workspace_once(workspace_id, funded, f"evt-gate-{unique}") is True
    _, key = native.create_api_key(
        workspace_id=workspace_id, creator_user_id=None, name=f"gate-{unique}",
    )
    before = native.typed_credit_snapshot(workspace_id)
    assert before is not None
    traffic = Traffic(native._database, monkeypatch)
    start = threading.Barrier(CLIENTS)

    def client(_index: int) -> None:
        start.wait(timeout=30)
        for _round in range(ROUNDS):
            _request(native, workspace_id, key.hash)

    with ThreadPoolExecutor(max_workers=CLIENTS) as pool:
        for future in [pool.submit(client, index) for index in range(CLIENTS)]:
            future.result(timeout=300)

    requests = CLIENTS * ROUNDS
    # At least one transaction of each kind per request (a store that keeps
    # legacy request records runs a second, sequential shape after the first).
    assert min(traffic.finished["tr_authorize"], traffic.finished["tr_finalize"]) >= requests
    # The money is the same with the gate off and on: every request charged
    # once, every hold released.
    credits, usage, reserved = native.typed_credit_snapshot(workspace_id)
    assert (credits, usage - before[1], reserved) == (before[0], CHARGE * requests, before[2])
    if gate_on:
        # One of the workspace's credit-row transactions at a time, so none
        # is aborted by another and redone.
        assert traffic.peak == 1
        assert traffic.redone == 0
    assert (gate.subjects(), gate.total_waiting()) == (0, 0)
