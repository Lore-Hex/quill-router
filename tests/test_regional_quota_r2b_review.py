"""Adversarial regressions for the R2b review findings."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from tests.test_regional_accounting_v2 import _totals
from tests.test_regional_quota_ledger import NOW, _FakeBigtableTable, _lease
from tests.test_regional_quota_r2b import authorize, escrow, global_lease, grant, setup
from trusted_router import storage_gcp_io as io
from trusted_router import storage_gcp_regional_quota as quota
from trusted_router.regional_quota_ledger import (
    BigtableRegionalQuotaLedger,
    InMemoryRegionalQuotaLedger,
    RegionalLeaseCasExhausted,
    RegionalLeaseLedgerError,
)
from trusted_router.services.regional_quota_leases import LeaseExhaustedError
from trusted_router.storage_gcp_counter_reconcile import audit_typed_invariants
from trusted_router.types import UsageType


def global_fallback(store: Any, args: dict[str, Any]) -> None:
    typed = {k: v for k, v in args.items() if not k.startswith("lease_") and k != "key_usage_shards"}
    result, auth = store.authorize_gateway_typed(
        authorization_id="global-fallback", **{**typed, "idempotency_key": "global-fallback"},
        has_credit_candidate=True, reservation_usage_type=UsageType.CREDITS, skip_key_limit=True,
    )
    assert result == "accepted" and auth is not None


@pytest.mark.parametrize("race", ["closed", "draining", "transport", "cas"])
def test_exhaustion_drain_race_still_allows_admission(
    monkeypatch: pytest.MonkeyPatch, race: str,
) -> None:
    store, db, _key, args = setup()
    first = authorize(store, args, "first")
    old = global_lease(store, first)
    ledger = store._regional_quota_ledger
    reserve, drain = ledger.reserve, ledger.begin_drain
    raced = False

    def competing_reserve(lease_id: str, **kw: Any) -> Any:
        nonlocal raced
        if lease_id == old.lease_id and not raced:
            raced = True
            local = ledger.get(lease_id, region=old.region)
            reserve(lease_id, **{**kw, "hold_id": "competitor", "fingerprint": "competitor",
                                "amount_microdollars": local.available_microdollars})
            # The request really loses the capacity race, rather than receiving
            # a synthetic exhaustion disconnected from the durable ledger.
            with pytest.raises(LeaseExhaustedError):
                reserve(lease_id, **kw)
            raise LeaseExhaustedError("competing hold consumed capacity")
        return reserve(lease_id, **kw)

    def concurrent_drain(lease_id: str, **kw: Any) -> Any:
        if lease_id == old.lease_id:
            if race == "transport":
                raise RegionalLeaseLedgerError("drain transport failed") from TimeoutError()
            if race == "cas":
                raise RegionalLeaseCasExhausted("drain contention")
            local = drain(lease_id, **kw)
            if race == "closed":
                for hold in local.holds:
                    local = ledger.refund(lease_id, region=old.region, hold_id=hold.hold_id,
                                          fencing_token=old.fencing_token)
                assert quota.reconcile_regional_quota_lease(store, old, local, close=True).closed
                ledger.close(lease_id, **kw)
        return drain(lease_id, **kw)

    monkeypatch.setattr(ledger, "reserve", competing_reserve)
    monkeypatch.setattr(ledger, "begin_drain", concurrent_drain)
    evidence: dict[str, Any] = {}
    result, auth = store.authorize_gateway_regional(
        authorization_id="loser", **{**args, "idempotency_key": "loser", "observation": evidence},
    )
    assert raced
    if race in {"closed", "draining"}:
        assert result == "accepted" and auth.regional_lease_id != old.lease_id
    else:
        assert (result, auth) == ("unavailable", None)
        assert evidence["regional_unavailable_reason"] == ("ledger_timeout" if race == "transport" else "other")
        global_fallback(store, args)
    assert audit_typed_invariants(store).clean
    assert escrow(db) > 0


@pytest.mark.parametrize("backend", ["memory", "bigtable"])
@pytest.mark.parametrize("field,value", [
    ("fencing_token", 10), ("workspace_id", "different-workspace"),
    ("granted_microdollars", 20_000), ("expires_at", NOW + timedelta(minutes=2)),
])
def test_initialize_rejects_conflicting_generation_without_changing_durable_state(
    backend: str, field: str, value: Any,
) -> None:
    ledger: Any = (InMemoryRegionalQuotaLedger() if backend == "memory" else
                   BigtableRegionalQuotaLedger({"us-central1": _FakeBigtableTable()}))
    original = _lease()
    ledger.initialize(original)
    ledger.reserve(original.lease_id, region=original.region, hold_id="durable", fingerprint="durable",
                   amount_microdollars=100, fencing_token=original.fencing_token,
                   key_hash="key", key_shard=0, now=NOW)
    durable = ledger.begin_drain(original.lease_id, region=original.region,
                                 fencing_token=original.fencing_token)
    with pytest.raises(RegionalLeaseLedgerError, match="different durable state"):
        ledger.initialize(replace(original, **{field: value}))
    assert ledger.get(original.lease_id, region=original.region) == durable
    assert len(durable.holds) == 1


def test_stale_retirement_preserves_reconciliation_cursor_with_open_hold() -> None:
    store, db, key, args = setup()
    first = authorize(store, args, "spent")
    authorize(store, args, "still-open")
    stale = global_lease(store, first)
    assert store.typed_finalize_gateway_authorization_result(
        first.id, success=True, actual_microdollars=123, selected_usage_type=UsageType.CREDITS,
    ).finalized
    ledger = store._regional_quota_ledger
    local = ledger.begin_drain(stale.lease_id, region=stale.region, fencing_token=stale.fencing_token)
    assert local.reserved_microdollars == args["estimate"]
    assert quota.reconcile_regional_quota_lease(store, stale, local, close=False).spent_delta_microdollars == 123
    before = _totals(db, key.workspace_id, key.hash)
    assert quota.retire_regional_quota_lease(store, stale, local)
    assert global_lease(store, first).state == "retiring"
    for _ in range(2):
        assert quota.reconcile_regional_quota_lease(store, stale, local, close=False).spent_delta_microdollars == 0
        assert _totals(db, key.workspace_id, key.hash) == before
    assert audit_typed_invariants(store).clean


@pytest.mark.parametrize("shards", [16, 64])
def test_cold_discovery_batches_complete_fence_keys(shards: int) -> None:
    store, db, _key, args = setup()
    authorize(store, args, "cold", lease_shard_count=shards)
    reads = [params for params in db.snapshot_sql_params if params.get("kind") == "regional_quota_fence"]
    assert len(reads) == 2
    assert len(reads[1]["ids"]) == shards - 1
    assert len(set(reads[1]["ids"]) | {reads[0]["id"]}) == shards


def test_slow_successes_share_one_regional_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.fakes.spanner import _FakeSnapshot

    store, db, key, args = setup()
    table = _FakeBigtableTable()
    ledger = BigtableRegionalQuotaLedger({args["region"]: table}, operation_timeout_seconds=4.0)
    store._regional_quota_ledger = ledger
    leases = []
    for shard in range(4):
        lease = grant(store, key.workspace_id, args["region"], quota_shard=shard,
                      adaptive=False, requested_microdollars=100_000, minimum_grant_microdollars=1)
        ledger.initialize(quota.regional_lease_from_global(lease))
        leases.append(quota.activate_regional_quota_lease(store, lease))

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Clock:
            return cls.fromtimestamp(leases[0].expires_datetime.timestamp() - 2, tz=tz)

    clock = [0.0]
    monkeypatch.setattr("trusted_router.storage_gcp.dt.datetime", Clock)
    monkeypatch.setattr(io.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr("trusted_router.regional_quota_ledger.monotonic", lambda: clock[0])
    read_sql, read_row, commit = _FakeSnapshot.execute_sql, table.read_row, ledger._commit_conditional_row
    read_budgets: list[float] = []
    commit_budgets: list[float] = []

    def slow_sql(snapshot: Any, *a: Any, **kw: Any) -> Any:
        clock[0] += 0.2
        return read_sql(snapshot, *a, **kw)

    def slow_read(*a: Any, **kw: Any) -> Any:
        budget = kw["retry"].deadline + 1.0
        read_budgets.append(budget)
        assert budget <= 4.0 - clock[0] + 1e-6
        clock[0] += 0.7
        return read_row(*a, **kw)

    def slow_commit(row: Any, *, timeout_seconds: float) -> bool:
        commit_budgets.append(timeout_seconds)
        assert timeout_seconds <= 4.0 - clock[0] + 1e-6
        clock[0] += min(0.4, timeout_seconds)
        return commit(row, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(_FakeSnapshot, "execute_sql", slow_sql)
    monkeypatch.setattr(table, "read_row", slow_read)
    monkeypatch.setattr(ledger, "_commit_conditional_row", slow_commit)
    evidence: dict[str, Any] = {}
    result = store.authorize_gateway_regional(authorization_id="slow", **{
        **args, "lease_shard_count": 4, "observation": evidence,
    })
    assert result == ("unavailable", None)
    assert evidence["regional_unavailable_reason"] == "ledger_timeout"
    assert read_budgets and commit_budgets
    assert 2.0 < clock[0] <= 4.0
    assert len(commit_budgets) < 4
    assert io.remaining_rpc_budget(20.0) == 20.0  # fallback has its own budget
    global_fallback(store, args)
    assert audit_typed_invariants(store).clean


@pytest.mark.parametrize("attempt_cost,prior_cost", [(0.0, 0.0), (3.8, 0.0), (3.8, 3.8)])
def test_spanner_retry_and_backoff_share_regional_deadline(
    monkeypatch: pytest.MonkeyPatch, attempt_cost: float, prior_cost: float,
) -> None:
    from google.api_core.exceptions import Aborted

    from tests.test_storage_gcp_io import _Clock, _install_clock, _TimedAbortingDatabase, _txn

    clock = _Clock()
    _install_clock(monkeypatch, clock)
    database = _TimedAbortingDatabase(clock, attempt_cost=attempt_cost)
    start = clock.now

    @io.spanner_rpc_budget(4.0)
    def retry() -> None:
        clock.now += prior_cost
        io.run_in_transaction_with_retry(database, _txn, attempts=1000)

    with pytest.raises(Aborted):
        retry()
    assert clock.now - start <= 4.0
    assert database.timeouts and all(0 < t <= 4.0 for t in database.timeouts if t is not None)
    assert io.remaining_rpc_budget(20.0) == 20.0


@pytest.mark.parametrize("final_budget", [0.5, 0.01])
def test_bigtable_initialization_and_reads_receive_remaining_shared_budget(
    monkeypatch: pytest.MonkeyPatch, final_budget: float,
) -> None:
    from tests.test_regional_quota_ledger import _FakeLegacyConditionalRow

    clock = [0.0]
    monkeypatch.setattr(io.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr("trusted_router.regional_quota_ledger.monotonic", lambda: clock[0])
    table = _FakeBigtableTable()
    ledger = BigtableRegionalQuotaLedger({"us-central1": table}, operation_timeout_seconds=4.0)
    ledger.initialize(_lease())
    row = _FakeLegacyConditionalRow()

    @io.spanner_rpc_budget(4.0)
    def attempt() -> None:
        clock[0] += 2.0
        assert ledger.get("rql-test", region="us-central1") == _lease()
        assert table.read_retry_deadlines[-1] == 1.0
        clock[0] = 4.0 - final_budget
        assert ledger._commit_conditional_row(row, timeout_seconds=4.0)
        assert row._table._instance._client.table_data_client.calls == [pytest.approx(final_budget)]

    attempt()


@pytest.mark.parametrize("error_kind", ["deadline", "retry"])
@pytest.mark.parametrize("stage", ["discovery", "grant", "activation", "retirement"])
def test_spanner_failure_during_regional_attempt_preserves_global_fallback(
    monkeypatch: pytest.MonkeyPatch, stage: str, error_kind: str,
) -> None:
    from google.api_core.exceptions import DeadlineExceeded, RetryError

    store, db, _key, args = setup()
    if stage == "retirement":
        for name in ("first", "second", "third"):
            authorize(store, args, name)
    pending_sibling = None
    if stage == "discovery":
        import hashlib

        selected_shard = int.from_bytes(
            hashlib.sha256(args["idempotency_fingerprint"].encode()).digest()[:4], "big",
        ) % args["lease_shard_count"]
        pending_sibling = grant(
            store, args["workspace_id"], args["region"],
            quota_shard=(selected_shard + 1) % args["lease_shard_count"],
        )
        assert pending_sibling.state == "pending"
    before = escrow(db)

    def timeout(*a: Any, **kw: Any) -> Any:
        if error_kind == "retry":
            raise RetryError("SDK retry exhaustion", DeadlineExceeded("regional attempt deadline"))
        raise DeadlineExceeded("regional attempt deadline")

    operation = {
        "discovery": "regional_quota_fences", "grant": "grant_regional_quota_lease",
        "activation": "activate_regional_quota_lease", "retirement": "retire_regional_quota_lease",
    }[stage]
    monkeypatch.setattr(quota, operation, timeout)
    evidence: dict[str, Any] = {}
    assert store.authorize_gateway_regional(authorization_id="deadline", **{
        **args, "observation": evidence,
    }) == ("unavailable", None)
    assert evidence["regional_unavailable_reason"] == ("other" if error_kind == "retry" else "ledger_timeout")
    if stage == "activation":
        pending = quota.active_regional_quota_leases(
            store, workspace_id=args["workspace_id"], region=args["region"],
            quota_shard=evidence["regional_selected_shard"], include_pending=True,
        )
        assert len(pending) == 1 and pending[0].state == "pending"
        assert escrow(db) == pending[0].granted_microdollars
    else:
        assert escrow(db) == before
    if pending_sibling is not None:
        assert quota.get_global_regional_quota_lease(
            store, workspace_id=args["workspace_id"], region=args["region"],
            lease_id=pending_sibling.lease_id,
        ) == pending_sibling
    global_fallback(store, args)
    assert audit_typed_invariants(store).clean


def test_configured_spanner_client_keeps_subsecond_remaining_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_storage_gcp_io import (
        _Clock,
        _CommitApi,
        _CommitDatabase,
        _install_clock,
        _txn,
    )

    clock = _Clock()
    _install_clock(monkeypatch, clock)
    database = _CommitDatabase(_CommitApi())
    io.configure_spanner_rpc_deadlines(database)

    @io.spanner_rpc_budget(4.0)
    def attempt() -> None:
        clock.now += 3.8
        assert io.run_in_transaction_with_retry(database, _txn) == "ok"

    attempt()
    assert database.timeouts == [pytest.approx(0.2)]
    assert database.spanner_api.calls[0]["timeout"] == pytest.approx(0.2)


@pytest.mark.parametrize("outcome", ["timeout", "recovery", "backoff"])
def test_real_bigtable_reader_retries_within_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    from functools import partial
    from types import SimpleNamespace

    from google.api_core import gapic_v1
    from google.api_core.exceptions import DeadlineExceeded, ServiceUnavailable
    from google.api_core.timeout import TimeToDeadlineTimeout
    from google.cloud.bigtable.table import Table
    from google.cloud.bigtable_v2.services.bigtable import BigtableClient

    clock = [0.0]
    timeouts: list[float] = []
    sleeps: list[float] = []
    monkeypatch.setattr(gapic_v1.method, "TimeToDeadlineTimeout", partial(
        TimeToDeadlineTimeout, clock=lambda: NOW + timedelta(seconds=clock[0]),
    ))
    monkeypatch.setattr(io.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr("trusted_router.regional_quota_ledger.monotonic", lambda: clock[0])

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(io.time, "sleep", sleep)
    monkeypatch.setattr("trusted_router.regional_quota_ledger.sleep", sleep)

    def transport(request: Any, *, timeout: float, **kw: Any) -> Any:
        timeouts.append(timeout)
        attempt = len(timeouts)

        def stream() -> Any:
            cost = 0.2 if attempt == 1 or outcome == "backoff" else (
                12.0 if outcome == "timeout" else 0.1
            )
            if outcome == "backoff" and attempt == 6:
                cost = 1.0  # leave less time than the next retry backoff
            clock[0] += min(cost, timeout)
            if cost >= timeout:
                raise DeadlineExceeded("transport timeout")
            if attempt == 1 or outcome == "backoff":
                raise ServiceUnavailable("stream interrupted")
            yield from ()

        return stream()

    # Real Table.read_row -> PartialRowsData -> GAPIC timeout wrapper. The SDK
    # restart without a timeout inherits this 12-hour default in the mutation.
    api = SimpleNamespace(read_rows=gapic_v1.method.wrap_method(
        transport, default_timeout=43_200.0,
    ), table_path=BigtableClient.table_path)
    table = Table("leases", SimpleNamespace(
        name="projects/test/instances/test", instance_id="test",
        _client=SimpleNamespace(
            project="test", table_data_client=api,
            _veneer_data_client=SimpleNamespace(get_table=lambda *a, **kw: None),
        ),
    ))
    ledger = BigtableRegionalQuotaLedger({"us-central1": table}, operation_timeout_seconds=4.0)

    @io.spanner_rpc_budget(4.0)
    def read() -> Any:
        return ledger._read_row(table, b"lease", filter_=None)

    if outcome == "recovery":
        assert read() is None
    else:
        with pytest.raises((DeadlineExceeded, TimeoutError)):
            read()
    assert len(timeouts) >= 2
    assert timeouts[0] == pytest.approx(4.0)
    assert 0 < timeouts[1] <= 3.8
    assert clock[0] <= 4.0
    assert sleeps and all(0 < delay <= 1.0 for delay in sleeps)


def test_no_candidate_trust_reread_shares_regional_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google.api_core.exceptions import DeadlineExceeded

    from tests.fakes.spanner import _FakeSnapshot

    store, db, key, args = setup()
    clock = [0.0]
    monkeypatch.setattr(io.time, "monotonic", lambda: clock[0])
    execute = _FakeSnapshot.execute_sql
    budgets: list[float] = []
    grant_finished = False

    def no_grant(*a: Any, **kw: Any) -> None:
        nonlocal grant_finished
        clock[0] += 3.8
        grant_finished = True

    def slow_query(snapshot: Any, *a: Any, **kw: Any) -> Any:
        if grant_finished:
            # Exercise the actual eligibility queries with transport latency
            # bounded as the configured Spanner RPC wrapper bounds it.
            budget = io.remaining_rpc_budget(20.0)
            budgets.append(budget)
            clock[0] += min(0.7, budget)
            if budget < 0.7:
                raise DeadlineExceeded("eligibility RPC timeout")
        return execute(snapshot, *a, **kw)

    monkeypatch.setattr(quota, "grant_regional_quota_lease", no_grant)
    monkeypatch.setattr(_FakeSnapshot, "execute_sql", slow_query)
    evidence: dict[str, Any] = {}

    @io.spanner_rpc_budget(25.0)
    def admission() -> None:
        assert store.authorize_gateway_regional(authorization_id="no-grant", **{
            **args, "observation": evidence,
        }) == ("unavailable", None)
        assert clock[0] == pytest.approx(4.0)
        assert budgets == [pytest.approx(0.2)]
        assert evidence["regional_unavailable_reason"] == "ledger_timeout"
        assert io.remaining_rpc_budget(25.0) == pytest.approx(21.0)

        # The exact fallback still reads authoritative trust: a pause written
        # after the regional timeout must refuse without reserving any money.
        nonlocal grant_finished
        grant_finished = False
        db.typed["tr_credit_balance"][(key.workspace_id, 0)]["billing_pause_causes"] = ["manual"]
        typed = {k: v for k, v in args.items() if not k.startswith("lease_") and k != "key_usage_shards"}
        result, auth = store.authorize_gateway_typed(
            authorization_id="paused-fallback", **typed,
            has_credit_candidate=True, reservation_usage_type=UsageType.CREDITS, skip_key_limit=True,
        )
        assert (result, auth) == ("billing_paused", None)
        assert escrow(db) == 0

    admission()
    assert audit_typed_invariants(store).clean
