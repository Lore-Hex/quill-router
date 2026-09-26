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


def global_fallback(store: Any, args: dict[str, Any], name: str = "failure") -> Any:
    typed = {k: v for k, v in args.items() if not k.startswith("lease_") and k != "key_usage_shards"}
    result, auth = store.authorize_gateway_typed(
        authorization_id=name, **{**typed, "idempotency_key": name},
        has_credit_candidate=True, reservation_usage_type=UsageType.CREDITS, skip_key_limit=True,
    )
    assert result == "accepted" and auth is not None
    return auth


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
        global_fallback(store, args, "loser")
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
    global_fallback(store, args, "slow")
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
    global_fallback(store, args, "deadline")
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
            authorization_id="no-grant", **typed,
            has_credit_candidate=True, reservation_usage_type=UsageType.CREDITS, skip_key_limit=True,
        )
        assert (result, auth) == ("billing_paused", None)
        assert escrow(db) == 0

    admission()
    assert audit_typed_invariants(store).clean


def _cooldown_failure(store: Any, args: dict[str, Any], name: str = "failure") -> str:
    evidence: dict[str, Any] = {}
    assert store.authorize_gateway_regional(
        authorization_id=name, **{**args, "idempotency_key": name}, observation=evidence,
    ) == ("unavailable", None)
    return str(evidence["regional_unavailable_reason"])


@pytest.mark.parametrize("site", ["get", "reserve", "begin_drain", "initialize", "sdk"])
@pytest.mark.parametrize("timeout", [True, False])
def test_ledger_cooldown_stops_storm(
    monkeypatch: pytest.MonkeyPatch, site: str, timeout: bool,
) -> None:
    from unittest.mock import Mock

    from google.api_core.exceptions import DeadlineExceeded, ServiceUnavailable

    store, _db, _key, args = setup()
    ledger = store._regional_quota_ledger
    if site in {"reserve", "begin_drain"}:
        authorize(store, args, "existing")
    if site == "begin_drain":
        args["estimate"] = 10_000_000  # Force retirement of insufficient local capacity.
    operation = getattr(ledger, "get" if site == "sdk" else site)
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        if site in {"reserve", "initialize", "begin_drain"}:
            operation(*_args, **_kwargs)  # Lose the response after the durable write.
        if site == "sdk":
            raise DeadlineExceeded("ledger timeout") if timeout else ServiceUnavailable("ledger down")
        cause = TimeoutError("ledger timeout") if timeout else ConnectionError("ledger down")
        raise RegionalLeaseLedgerError("ledger transport") from cause

    monkeypatch.setattr(ledger, "get" if site == "sdk" else site, fail)
    assert _cooldown_failure(store, args) == ("ledger_timeout" if timeout else "other")
    spies = []
    for method in ("get", "initialize", "reserve", "begin_drain", "close", "settle", "refund"):
        spy = Mock(wraps=getattr(ledger, method))
        monkeypatch.setattr(ledger, method, spy)
        spies.append(spy)
    for method in ("grant_regional_quota_lease", "retire_regional_quota_lease",
                   "quarantine_regional_quota_lease", "active_regional_quota_leases",
                   "regional_quota_fences"):
        spy = Mock(wraps=getattr(quota, method))
        monkeypatch.setattr(quota, method, spy)
        spies.append(spy)
    assert _cooldown_failure(store, args, "next") == "ledger_cooldown"
    for spy in spies:
        spy.assert_not_called()
    global_fallback(store, args)


def test_ledger_cooldown_is_scoped_to_workspace_and_region(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_trust_eligibility_pr2 import regional_args, workspace_state

    store, db, _key, args = setup()
    ledger = store._regional_quota_ledger
    with monkeypatch.context() as patch:
        def fail(*_args: Any, **_kwargs: Any) -> Any:
            raise RegionalLeaseLedgerError("transport") from TimeoutError()
        patch.setattr(ledger, "get", fail)
        assert _cooldown_failure(store, args) == "ledger_timeout"
    authorize(store, args, "other-region", region="us-east4")
    ws = store.create_workspace("owner", "other", trial_credit_microdollars=0)
    workspace_state(db, 1, ws.id)["total_credits"] = 100_000_000
    _, key = store.create_api_key(workspace_id=ws.id, name="other", creator_user_id="owner")
    authorize(store, regional_args(ws.id, key), "other-workspace")
    assert _cooldown_failure(store, args, "still-blocked") == "ledger_cooldown"


@pytest.mark.parametrize("base", [1.0, 10.0, 60.0])
@pytest.mark.parametrize("jitter", [0.75, 1.0, 1.25])
def test_ledger_cooldown_expiry_and_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch, base: float, jitter: float,
) -> None:
    store, _db, _key, args = setup()
    store.trust_settings.regional_quota_ledger_cooldown_seconds = base
    clock = [1000.0]
    monkeypatch.setattr("trusted_router.storage_gcp.time.monotonic", lambda: clock[0])
    def uniform(low: float, high: float) -> float:
        assert (low, high) == (0.75, 1.25)
        return jitter
    monkeypatch.setattr("trusted_router.storage_gcp.random.uniform", uniform)
    calls = []
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        calls.append(clock[0])
        raise RegionalLeaseLedgerError("transport") from TimeoutError()
    monkeypatch.setattr(store._regional_quota_ledger, "get", fail)
    for index in range(8):
        assert _cooldown_failure(store, args, f"failure-{index}") == "ledger_timeout"
        deadline = clock[0] + min(60.0, min(60.0, base * 2 ** index) * jitter)
        clock[0] = deadline - 0.001
        assert _cooldown_failure(store, args, f"blocked-{index}") == "ledger_cooldown"
        assert len(calls) == index + 1
        clock[0] = deadline


def test_ledger_cooldown_success_clears_inflight_probe_and_resets_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _db, _key, args = setup()
    clock = [1000.0]
    monkeypatch.setattr("trusted_router.storage_gcp.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("trusted_router.storage_gcp.random.uniform", lambda _a, _b: 1.0)
    ledger = store._regional_quota_ledger
    reserve = ledger.reserve
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise RegionalLeaseLedgerError("transport") from TimeoutError()
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "get", fail)
        assert _cooldown_failure(store, args, "first") == "ledger_timeout"
        clock[0] += 10
        assert _cooldown_failure(store, args, "second") == "ledger_timeout"
    clock[0] += 20
    # The half-open probe excludes concurrent attempts until recording succeeds.
    def reserve_with_concurrent_failure(*a: Any, **kw: Any) -> Any:
        with monkeypatch.context() as patch:
            patch.setattr(ledger, "get", fail)
            assert _cooldown_failure(store, args, "concurrent") == "ledger_cooldown"
        return reserve(*a, **kw)
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "reserve", reserve_with_concurrent_failure)
        authorize(store, args, "success")
    authorize(store, args, "immediately-after-success")
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "get", fail)
        assert _cooldown_failure(store, args, "reset-failure") == "ledger_timeout"
    clock[0] += 9.999
    assert _cooldown_failure(store, args, "blocked") == "ledger_cooldown"
    clock[0] += 0.001
    authorize(store, args, "reset-window-elapsed")


@pytest.mark.parametrize("success", [True, False])
def test_ledger_cooldown_does_not_block_finalize_or_reconcile(
    monkeypatch: pytest.MonkeyPatch, success: bool,
) -> None:
    store, db, key, args = setup()
    auth = authorize(store, args, "hold")
    ledger = store._regional_quota_ledger
    reserved = ledger.get(auth.regional_lease_id, region=auth.region)
    monkeypatch.setattr("trusted_router.storage_gcp.time.monotonic", lambda: 1000.0)
    with monkeypatch.context() as patch:
        def fail(*_args: Any, **_kwargs: Any) -> Any:
            raise RegionalLeaseLedgerError("transport") from TimeoutError()
        patch.setattr(ledger, "get", fail)
        assert _cooldown_failure(store, args) == "ledger_timeout"
    assert store.typed_finalize_gateway_authorization_result(
        auth.id, success=success, actual_microdollars=123,
        selected_usage_type=UsageType.CREDITS,
    ).finalized
    local = ledger.get(auth.regional_lease_id, region=auth.region)
    hold = next(hold for hold in local.holds if hold.hold_id == auth.id)
    assert hold.state.value == ("settled" if success else "refunded")
    assert local.spent_microdollars == (123 if success else 0)
    # Simulate an unapplied terminal ledger transition: the REAL worker must
    # recover the typed terminal outcome even while admission is cooled down.
    ledger._leases[(auth.region, auth.regional_lease_id)] = reserved
    now = args["expires_at"] + timedelta(hours=1)
    result = store.reconcile_regional_quota_leases(now=now)
    assert result["errors"] == 0 and result["closed"] == result["reconciled"] == 1
    local = ledger.get(auth.regional_lease_id, region=auth.region)
    assert local.state.value == "closed" and local.reserved_microdollars == 0
    assert local.holds[0].state.value == ("settled" if success else "refunded")
    assert global_lease(store, auth).state == "closed"
    assert escrow(db) == 0
    assert _totals(db, key.workspace_id, key.hash) == ((123 if success else 0),) * 5
    assert store.reconcile_regional_quota_leases(now=now)["errors"] == 0
    assert escrow(db) == 0
    assert audit_typed_invariants(store).clean
    assert _cooldown_failure(store, args, "still-blocked") == "ledger_cooldown"


def _only_lease(store: Any) -> Any:
    leases = store._list_entities("regional_quota_lease", cls=quota.GlobalRegionalQuotaLease)
    assert len(leases) == 1
    return leases[0]


def _fence(store: Any, lease: Any) -> Any:
    return quota.regional_quota_fences(
        store, workspace_id=lease.workspace_id, region=lease.region,
        quota_shards=[lease.quota_shard],
    )[lease.quota_shard]


def test_ledger_cooldown_initialization_ambiguity_quarantines_and_arms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _key, args = setup()
    ledger = store._regional_quota_ledger
    initialize = ledger.initialize
    def fail(*a: Any, **kw: Any) -> Any:
        initialize(*a, **kw)  # Durable row exists, but the response is lost.
        raise RuntimeError("ambiguous initialization")
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "initialize", fail)
        assert _cooldown_failure(store, args) == "other"
    lease = _only_lease(store)
    assert lease.state == "quarantined"
    assert "initialization ambiguity" in lease.last_error
    fence = _fence(store, lease)
    assert (fence.active_lease_id, fence.fencing_token) == (lease.lease_id, lease.fencing_token)
    assert escrow(db) == lease.granted_microdollars > 0
    assert ledger.get(lease.lease_id, region=lease.region).state.value == "active"
    assert _cooldown_failure(store, args, "next") == "ledger_cooldown"
    result = store.reconcile_regional_quota_leases(now=args["expires_at"] + timedelta(hours=1))
    assert result["errors"] == 0 and result["closed"] == 1
    assert _only_lease(store).state == "closed" and escrow(db) == 0
    assert _fence(store, lease).active_lease_id is None
    assert audit_typed_invariants(store).clean


def test_ledger_cooldown_evicts_only_cold_history(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _db, _key, args = setup()
    monkeypatch.setattr("trusted_router.storage_gcp.time.monotonic", lambda: 1000.0)
    store._regional_ledger_cooldowns = {(str(n), "region"): (939.0, 60.0, 0) for n in range(10_000)}
    store._regional_ledger_cooldowns[("active", "region")] = (1001.0, 10.0, 0)
    store._regional_ledger_cooldowns[("recent", "region")] = (999.0, 20.0, 0)
    store._arm_regional_ledger_cooldown(args["workspace_id"], args["region"])
    assert set(store._regional_ledger_cooldowns) == {
        ("active", "region"), ("recent", "region"), (args["workspace_id"], args["region"]),
    }


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_ledger_cooldown_concurrent_expiry_claims_one_probe(
    monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor, wait

    store, _db, _key, args = setup()
    clock = [1000.0]
    monkeypatch.setattr("trusted_router.storage_gcp.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("trusted_router.storage_gcp.random.uniform", lambda _a, _b: 1.0)
    store._arm_regional_ledger_cooldown(args["workspace_id"], args["region"])
    clock[0] += 10
    entered, release = threading.Event(), threading.Event()
    start = threading.Barrier(8)
    grant_real = quota.grant_regional_quota_lease
    attempts: list[int] = []
    def blocked_grant(*a: Any, **kw: Any) -> Any:
        attempts.append(1)
        entered.set()
        assert release.wait(10)
        if outcome == "failure":
            from google.api_core.exceptions import DeadlineExceeded
            raise DeadlineExceeded("probe failed")
        return grant_real(*a, **kw)
    monkeypatch.setattr(quota, "grant_regional_quota_lease", blocked_grant)
    def request(n: int) -> Any:
        start.wait(10)
        evidence: dict[str, Any] = {}
        result = store.authorize_gateway_regional(
            authorization_id=f"probe-{n}", **{**args, "idempotency_key": f"probe-{n}"},
            observation=evidence,
        )
        if result == ("unavailable", None) and evidence["regional_unavailable_reason"] == "ledger_cooldown":
            fallback = global_fallback(store, args, f"probe-{n}")
            assert fallback.settlement == "local"
        return result, evidence
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(request, n) for n in range(8)]
        try:
            assert entered.wait(10)
            done, _ = wait(futures, timeout=1)
            assert len(done) == 7
            assert len(attempts) == 1
            assert all(f.result()[1]["regional_unavailable_reason"] == "ledger_cooldown" for f in done)
        finally:
            release.set()
        results = [f.result() for f in futures]
    assert len(attempts) == 1
    assert sum(result[0][0] == "accepted" for result in results) == (outcome == "success")
    if outcome == "success":
        assert not store._regional_ledger_cooldown_active(args["workspace_id"], args["region"])
    else:
        assert store._regional_ledger_cooldowns[(args["workspace_id"], args["region"])][:2] == (1030.0, 20.0)


@pytest.mark.parametrize("initial_cooldown", [True, False])
def test_ledger_cooldown_delayed_recording_preserves_newer_failure(
    monkeypatch: pytest.MonkeyPatch, initial_cooldown: bool,
) -> None:
    store, _db, _key, args = setup()
    clock = [1000.0]
    monkeypatch.setattr("trusted_router.storage_gcp.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("trusted_router.storage_gcp.random.uniform", lambda _a, _b: 1.0)
    key = (args["workspace_id"], args["region"])
    if initial_cooldown:
        store._arm_regional_ledger_cooldown(*key)
    clock[0] = 1010.0
    ledger = store._regional_quota_ledger
    record = quota.record_regional_gateway_authorization
    failures: list[float] = []

    def fail(*a: Any, **kw: Any) -> Any:
        failures.append(clock[0])
        raise RegionalLeaseLedgerError("transport") from TimeoutError()

    def delayed_record(*a: Any, **kw: Any) -> Any:
        # A has reserved successfully, but Spanner recording is outside the
        # four-second budget. Let B replace the five-second claim and fail.
        auth = kw["authorization"]
        local = ledger.get(auth.regional_lease_id, region=auth.region)
        assert any(hold.hold_id == auth.id for hold in local.holds)
        clock[0] = 1015.1
        monkeypatch.setattr(ledger, "get", fail)
        assert _cooldown_failure(store, args, "probe-b") == "ledger_timeout"
        return record(*a, **kw)

    monkeypatch.setattr(quota, "record_regional_gateway_authorization", delayed_record)
    authorize(store, args, "probe-a")
    assert _cooldown_failure(store, args, "after-a") == "ledger_cooldown"
    assert failures == [1015.1]
    window = 20.0 if initial_cooldown else 10.0
    assert store._regional_ledger_cooldowns[key][:2] == (1015.1 + window, window)


def test_ledger_cooldown_generation_survives_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _db, _key, args = setup()
    clock = [1000.0]
    monkeypatch.setattr("trusted_router.storage_gcp.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("trusted_router.storage_gcp.random.uniform", lambda _a, _b: 1.0)
    key = (args["workspace_id"], args["region"])
    store._arm_regional_ledger_cooldown(*key)
    clock[0] = 1010.0
    stale = store._claim_regional_ledger_probe(*key)
    assert stale is not None
    clock[0] = 1015.1
    authorize(store, args, "replacement")  # Clears the old entry.
    store._arm_regional_ledger_cooldown(*key)
    clock[0] += 10.0
    current = store._claim_regional_ledger_probe(*key)
    assert current is not None and current > stale
    store._clear_regional_ledger_cooldown(*key, stale)
    assert _cooldown_failure(store, args, "still-claimed") == "ledger_cooldown"
    store._clear_regional_ledger_cooldown(*key, current)
    authorize(store, args, "recovered")


def test_ledger_cooldown_abandoned_probe_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _db, _key, args = setup()
    clock = [1000.0]
    monkeypatch.setattr("trusted_router.storage_gcp.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("trusted_router.storage_gcp.random.uniform", lambda _a, _b: 1.0)
    store._arm_regional_ledger_cooldown(args["workspace_id"], args["region"])
    clock[0] += 10
    # Claim with no completion (no success clear or failure re-arm).
    assert store._claim_regional_ledger_probe(args["workspace_id"], args["region"])
    clock[0] += 4.999
    assert _cooldown_failure(store, args) == "ledger_cooldown"
    clock[0] += 0.001
    authorize(store, args, "replacement-probe")


@pytest.mark.parametrize("success", [True, False])
def test_ledger_cooldown_durable_reserve_same_id_fallback_recovers(
    monkeypatch: pytest.MonkeyPatch, success: bool,
) -> None:
    store, db, key, args = setup()
    ledger = store._regional_quota_ledger
    reserve = ledger.reserve
    def fail(*a: Any, **kw: Any) -> Any:
        reserve(*a, **kw)
        raise RegionalLeaseLedgerError("response lost") from TimeoutError()
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "reserve", fail)
        assert _cooldown_failure(store, args) == "ledger_timeout"
    lease = _only_lease(store)
    local = ledger.get(lease.lease_id, region=lease.region)
    assert local.reserved_microdollars == 10_000
    auth = global_fallback(store, args, "failure")
    assert auth.id == local.holds[0].hold_id and auth.settlement == "local"
    # Even after the regional hold expires, a live global request is not
    # terminal refund authority. Preserve its unused local reservation.
    now = args["expires_at"] + timedelta(hours=1)
    assert store.reconcile_regional_quota_leases(now=now)["closed"] == 0
    assert ledger.get(lease.lease_id, region=lease.region).reserved_microdollars == 10_000
    assert store.typed_finalize_gateway_authorization_result(
        auth.id, success=success, actual_microdollars=123, selected_usage_type=UsageType.CREDITS,
    ).finalized
    # Differential money check against an otherwise identical global request.
    reference, reference_db, reference_key, reference_args = setup()
    reference_auth = global_fallback(reference, reference_args)
    assert reference.typed_finalize_gateway_authorization_result(
        reference_auth.id, success=success, actual_microdollars=123,
        selected_usage_type=UsageType.CREDITS,
    ).finalized
    expected = _totals(reference_db, reference_key.workspace_id, reference_key.hash)
    assert expected == ((123 if success else 0),) * 5
    assert _totals(db, key.workspace_id, key.hash) == expected
    now = args["expires_at"] + timedelta(hours=1)
    for _ in range(2):
        result = store.reconcile_regional_quota_leases(now=now)
        assert result["errors"] == 0
        local = ledger.get(lease.lease_id, region=lease.region)
        assert local.state.value == "closed" and local.reserved_microdollars == 0
        assert local.holds[0].state.value == "refunded" and local.spent_microdollars == 0
        assert _only_lease(store).state == "closed" and escrow(db) == 0
        assert _fence(store, lease).active_lease_id is None
        assert _totals(db, key.workspace_id, key.hash) == expected
        assert audit_typed_invariants(store).clean


def test_ledger_cooldown_expired_pending_generation_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.api_core.exceptions import DeadlineExceeded

    store, db, key, args = setup()
    ledger = store._regional_quota_ledger
    grant_real = quota.grant_regional_quota_lease
    def fail(*a: Any, **kw: Any) -> Any:
        grant_real(*a, **kw)
        raise DeadlineExceeded("grant committed but reply lost before initialization")
    with monkeypatch.context() as patch:
        patch.setattr(quota, "grant_regional_quota_lease", fail)
        assert _cooldown_failure(store, args) == "ledger_timeout"
    lease = _only_lease(store)
    assert lease.state == "pending" and escrow(db) == lease.granted_microdollars == 40_000
    assert ledger.get(lease.lease_id, region=lease.region) is None
    assert store.reconcile_regional_quota_leases()["closed"] == 0  # live issuer is untouched
    now = args["expires_at"] + timedelta(hours=1)
    for _ in range(2):
        result = store.reconcile_regional_quota_leases(now=now)
        assert result["errors"] == 0
        assert _only_lease(store).state == "closed" and escrow(db) == 0
        assert _fence(store, lease).active_lease_id is None
        assert _totals(db, key.workspace_id, key.hash) == (0,) * 5
        assert audit_typed_invariants(store).clean
    # A suspended issuer cannot activate or recreate a spendable generation.
    assert quota.activate_regional_quota_lease(store, lease).state == "closed"
    assert ledger.initialize(quota.regional_lease_from_global(lease)).state.value == "closed"


@pytest.mark.parametrize("field,value", [
    ("workspace_id", "wrong"), ("region", "wrong"), ("key_hash", "wrong"),
    ("idempotency_fingerprint", "wrong"), ("estimated_microdollars", 1),
    ("regional_lease_id", "wrong"), ("regional_fencing_token", 99),
    ("regional_hold_id", "wrong"), ("settlement", "deferred_home"),
    ("reservation.workspace_id", "wrong"), ("reservation.key_hash", "wrong"),
    ("reservation.authorization_id", "wrong"), ("reservation.hold_usage_type", "RegionalCredits"),
])
def test_bound_global_fallback_rejects_mismatch(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any,
) -> None:
    from trusted_router import storage_gcp_counter_dml as counters
    from trusted_router import storage_gcp_request_records as records

    store, db, _key, args = setup()
    lease = grant(store, args["workspace_id"], args["region"])
    ledger = store._regional_quota_ledger
    ledger.initialize(quota.regional_lease_from_global(lease))
    local = ledger.reserve(
        lease.lease_id, region=lease.region, hold_id="failure",
        fingerprint=args["idempotency_fingerprint"], amount_microdollars=args["estimate"],
        fencing_token=lease.fencing_token, key_hash=args["key_hash"], key_shard=0,
        hold_expires_at=args["expires_at"],
    )
    auth = global_fallback(store, args)
    assert store.typed_finalize_gateway_authorization_result(
        auth.id, success=True, actual_microdollars=123, selected_usage_type=UsageType.CREDITS,
    ).finalized
    assert quota.terminal_regional_hold_amount(store, lease, auth.id, hold=local.holds[0]) == 0
    read_auth, read_reservation = records.read_gateway_authorization, counters.read_reservation
    if field.startswith("reservation."):
        def corrupt_reservation(*a: Any, **kw: Any) -> Any:
            reservation = read_reservation(*a, **kw)
            assert reservation is not None
            return {**reservation, field.split(".")[1]: value}
        monkeypatch.setattr(counters, "read_reservation", corrupt_reservation)
    else:
        def corrupt_auth(*a: Any, **kw: Any) -> Any:
            authorization = read_auth(*a, **kw)
            assert authorization is not None
            return replace(authorization, **{field: value})
        monkeypatch.setattr(records, "read_gateway_authorization", corrupt_auth)
    before = escrow(db)
    with pytest.raises(RuntimeError, match="binding mismatch"):
        quota.terminal_regional_hold_amount(store, lease, auth.id, hold=local.holds[0])
    assert ledger.get(lease.lease_id, region=lease.region).reserved_microdollars == args["estimate"]
    assert escrow(db) == before > 0


def test_expired_pending_recovery_does_not_override_concurrent_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _key, args = setup()
    lease = grant(store, args["workspace_id"], args["region"])
    ledger = store._regional_quota_ledger
    transition = quota._transition_global_lease
    def activate_before_recovery(*a: Any, **kw: Any) -> Any:
        ledger.initialize(quota.regional_lease_from_global(lease))
        transition(store, lease, expected_states={"pending"}, state="active")
        return transition(*a, **kw)
    monkeypatch.setattr(quota, "_transition_global_lease", activate_before_recovery)
    with pytest.raises(RuntimeError, match="regional lease is active"):
        quota.close_expired_uninitialized_regional_quota_lease(
            store, lease, now=lease.expires_datetime + timedelta(seconds=1),
        )
    assert _only_lease(store).state == "active"
    assert _fence(store, lease).active_lease_id == lease.lease_id
    assert escrow(db) == lease.granted_microdollars
