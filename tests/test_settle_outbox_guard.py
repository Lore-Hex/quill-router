"""Durable settle outbox — Increment 2: reaper guard wiring.

Mirrors the typed-billing reaper helpers from test_billing_typed_enforcement and
the outbox row builder style from test_settle_outbox_storage.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from tests.test_billing_typed_enforcement import (
    _NOW,
    _authorize,
    _make_key,
    _seed_credit,
    _typed,
)
from trusted_router import storage_gcp_authorize as authorize_mod
from trusted_router.storage_gcp_authorize import (
    _REAP_SCAN_GUARDED_SQL,
    _REAP_SCAN_SQL,
    AuthorizeOutcome,
    SettleOutcome,
    reap_expired_reservations,
    settle_atomic,
    typed_finalize_atomic,
)
from trusted_router.storage_gcp_counter_dml import complete_reservation_retention
from trusted_router.storage_gcp_request_records import (
    complete_gateway_authorization_retention,
)
from trusted_router.storage_gcp_settle_outbox import (
    GUARD_COUNT_SQL,
    GUARD_STATUSES,
    SpannerSettleOutbox,
)
from trusted_router.storage_models import SettleOutboxRow


def _outbox(store: Any) -> SpannerSettleOutbox:
    return SpannerSettleOutbox(store._database, store._param_types)


class _ProxySnapshot:
    def __init__(self, inner: Any, on_execute: Any) -> None:
        self._inner = inner
        self._on_execute = on_execute
        self._snapshot: Any = None

    def __enter__(self) -> _ProxySnapshot:
        self._snapshot = self._inner.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._inner.__exit__(*args)

    def execute_sql(
        self,
        sql: str,
        *,
        params: dict[str, Any] | None = None,
        param_types: Any = None,
    ) -> list[list[Any]]:
        replacement = self._on_execute(sql)
        if isinstance(replacement, str):
            sql = replacement
        return self._snapshot.execute_sql(sql, params=params, param_types=param_types)


@pytest.fixture(autouse=True)
def _clear_outbox_availability_cache() -> Any:
    """The availability cache is a process-global dict keyed by database name, so
    one test's cached present/absent verdict must not leak into another."""
    authorize_mod._OUTBOX_AVAILABILITY_CACHE.clear()
    yield
    authorize_mod._OUTBOX_AVAILABILITY_CACHE.clear()


class _ProxyDatabase:
    def __init__(self, inner: Any, on_execute: Any) -> None:
        self._inner = inner
        self._on_execute = on_execute

    def snapshot(self, **kwargs: Any) -> _ProxySnapshot:
        return _ProxySnapshot(self._inner.snapshot(**kwargs), self._on_execute)

    def run_in_transaction(
        self,
        fn: Any,
        *,
        timeout_secs: Any = None,
        transaction_tag: str | None = None,
    ) -> Any:
        def proxied_fn(transaction: Any) -> Any:
            return fn(_ProxyTransaction(transaction, self._on_execute))

        return self._inner.run_in_transaction(
            proxied_fn,
            timeout_secs=timeout_secs,
            transaction_tag=transaction_tag,
        )


class _ProxyTransaction:
    def __init__(self, inner: Any, on_execute: Any) -> None:
        self._inner = inner
        self._on_execute = on_execute

    def execute_sql(
        self,
        sql: str,
        *,
        params: dict[str, Any] | None = None,
        param_types: Any = None,
    ) -> list[list[Any]]:
        replacement = self._on_execute(sql)
        if isinstance(replacement, str):
            sql = replacement
        return self._inner.execute_sql(sql, params=params, param_types=param_types)

    def execute_update(
        self,
        sql: str,
        *,
        params: dict[str, Any] | None = None,
        param_types: Any = None,
    ) -> int:
        replacement = self._on_execute(sql)
        if isinstance(replacement, str):
            sql = replacement
        return int(
            self._inner.execute_update(
                sql,
                params=params,
                param_types=param_types,
            )
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _row(aid: str, rid: str, *, cost: int = 900_000) -> SettleOutboxRow:
    return SettleOutboxRow(
        authorization_id=aid,
        intent_kind="settle",
        settle_origin="typed",
        actual_cost_micro=cost,
        reservation_id=rid,
        selected_endpoint_id="openai/gpt-4o@openai",
        model_id="openai/gpt-4o",
        selected_usage_type="Credits",
        settle_body=f'{{"authorization_id":"{aid}","reservation_id":"{rid}"}}',
    )


def _expired_authorization(store: Any, *, ws: str, estimate: int = 1_000_000) -> dict[str, Any]:
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    auth = _authorize(store, ws=ws, key_hash=key.hash, estimate=estimate)
    assert auth["outcome"] == AuthorizeOutcome.ACCEPTED
    return auth


def _assert_frozen(db: Any, ws: str, rid: str) -> None:
    assert _typed(db, ws)["reserved"] == 1_000_000
    assert db.reservations[rid]["settled"] is False


def _assert_free_released(db: Any, ws: str, rid: str) -> None:
    assert _typed(db, ws)["reserved"] == 0
    assert _typed(db, ws)["total_usage"] == 0
    assert db.reservations[rid]["settled"] is True
    assert db.reservations[rid]["actual_micro"] == 0


def test_guard_sql_literals_come_from_guard_statuses() -> None:
    for sql in (GUARD_COUNT_SQL, _REAP_SCAN_GUARDED_SQL):
        [fragment] = re.findall(r"\bstatus IN \(([^)]*)\)", sql)
        quoted = re.findall(r"'([^']+)'", sql)
        assert quoted == list(GUARD_STATUSES)
        assert re.findall(r"'([^']+)'", fragment) == quoted


def test_pending_row_freezes_hold() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_guard_pending"
    auth = _expired_authorization(store, ws=ws)
    rid, aid = auth["reservation_id"], auth["authorization_id"]
    _outbox(store).enqueue(_row(aid, rid))

    assert reap_expired_reservations(store._database, store._param_types, now=_NOW) == 0
    _assert_frozen(db, ws, rid)
    assert reap_expired_reservations(store._database, store._param_types, now=_NOW) == 0
    _assert_frozen(db, ws, rid)


def test_dead_row_freezes_hold() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_guard_dead"
    auth = _expired_authorization(store, ws=ws)
    rid, aid = auth["reservation_id"], auth["authorization_id"]
    ob = _outbox(store)
    ob.enqueue(_row(aid, rid))
    [job] = ob.claim()
    assert ob.mark(aid, "settle", done=False, lease_owner=job.lease_owner, max_attempts=1) == "dead"

    assert reap_expired_reservations(store._database, store._param_types, now=_NOW) == 0
    _assert_frozen(db, ws, rid)


def test_release_approved_row_is_reaped() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_guard_release_approved"
    auth = _expired_authorization(store, ws=ws)
    rid, aid = auth["reservation_id"], auth["authorization_id"]
    _outbox(store).enqueue(_row(aid, rid))
    db.settle_outbox[(aid, "settle")]["status"] = "release_approved"

    assert reap_expired_reservations(store._database, store._param_types, now=_NOW) == 1
    _assert_free_released(db, ws, rid)


def test_done_row_is_reaped() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_guard_done"
    auth = _expired_authorization(store, ws=ws)
    rid, aid = auth["reservation_id"], auth["authorization_id"]
    ob = _outbox(store)
    ob.enqueue(_row(aid, rid))
    assert ob.mark(aid, "settle", done=True) == "done"

    assert reap_expired_reservations(store._database, store._param_types, now=_NOW) == 1
    _assert_free_released(db, ws, rid)


def test_empty_table_reaper_unchanged() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_guard_empty"
    auth = _expired_authorization(store, ws=ws)
    rid = auth["reservation_id"]

    assert reap_expired_reservations(store._database, store._param_types, now=_NOW) == 1
    _assert_free_released(db, ws, rid)


def test_in_txn_guard_beats_stale_advisory() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_guard_mf2"
    auth = _expired_authorization(store, ws=ws)
    rid, aid = auth["reservation_id"], auth["authorization_id"]
    _outbox(store).enqueue(_row(aid, rid))

    fired = []

    def stale_scan(sql: str) -> str | None:
        if sql == _REAP_SCAN_GUARDED_SQL:
            fired.append(True)
            return _REAP_SCAN_SQL
        return None

    proxied = _ProxyDatabase(store._database, stale_scan)

    assert reap_expired_reservations(proxied, store._param_types, now=_NOW) == 0
    assert fired
    _assert_frozen(db, ws, rid)


def test_settle_atomic_guard_outbox_semantics() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_guard_settle_atomic"
    auth = _expired_authorization(store, ws=ws)
    rid, aid = auth["reservation_id"], auth["authorization_id"]
    _outbox(store).enqueue(_row(aid, rid))

    guarded = settle_atomic(
        store._database,
        store._param_types,
        reservation_id=rid,
        actual_micro=0,
        settled_usage_type="Credits",
        success=False,
        guard_outbox=True,
    )
    assert guarded["outcome"] == SettleOutcome.OUTBOX_GUARDED
    _assert_frozen(db, ws, rid)

    inline = settle_atomic(
        store._database,
        store._param_types,
        reservation_id=rid,
        actual_micro=900_000,
        settled_usage_type="Credits",
        success=True,
        guard_outbox=False,
    )
    assert inline["outcome"] == SettleOutcome.SETTLED
    assert _typed(db, ws)["reserved"] == 0
    assert _typed(db, ws)["total_usage"] == 900_000


def test_probe_falls_back_when_table_missing() -> None:
    class NotFound(Exception):
        pass

    def missing_table(sql: str) -> None:
        if "tr_settle_outbox" in sql:
            raise NotFound("Table not found: tr_settle_outbox")
        return None

    store, db, _ = make_fake_store()
    ws = "ws_guard_missing_table"
    auth = _expired_authorization(store, ws=ws)
    rid = auth["reservation_id"]

    proxied = _ProxyDatabase(store._database, missing_table)

    assert reap_expired_reservations(proxied, store._param_types, now=_NOW) == 1
    _assert_free_released(db, ws, rid)
    assert db.reservations[rid]["terminal_at"] is not None


def test_settle_claim_arms_retention_when_outbox_table_missing() -> None:
    class NotFound(Exception):
        pass

    probe_calls = 0

    def missing_table(sql: str) -> None:
        nonlocal probe_calls
        if "tr_settle_outbox" in sql:
            probe_calls += 1
            raise NotFound("Table not found: tr_settle_outbox")
        return None

    store, db, _ = make_fake_store()
    first = _expired_authorization(store, ws="ws_missing_claim_first")
    second = _expired_authorization(store, ws="ws_missing_claim_second")
    proxied = _ProxyDatabase(store._database, missing_table)
    proxied.name = "projects/p/instances/i/databases/missing-claim"

    for auth in (first, second):
        result = settle_atomic(
            proxied,
            store._param_types,
            reservation_id=auth["reservation_id"],
            actual_micro=900_000,
            settled_usage_type="Credits",
            success=True,
        )
        assert result["outcome"] == SettleOutcome.SETTLED
        assert db.reservations[auth["reservation_id"]]["terminal_at"] is not None

    # The absent result is briefly cached: pre-DDL traffic does not add one
    # availability read to every hot settle request.
    assert probe_calls == 1


def test_settle_probe_transient_fails_toward_guarded_sql() -> None:
    class NotFound(Exception):
        pass

    claim_sql: list[str] = []

    def transient_probe(sql: str) -> None:
        if sql == GUARD_COUNT_SQL:
            raise NotFound("Session not found while querying tr_settle_outbox")
        if "UPDATE tr_reservation SET settled=true" in sql:
            claim_sql.append(sql)
        return None

    store, db, _ = make_fake_store()
    auth = _expired_authorization(store, ws="ws_transient_claim_probe")
    rid = auth["reservation_id"]
    proxied = _ProxyDatabase(store._database, transient_probe)

    result = settle_atomic(
        proxied,
        store._param_types,
        reservation_id=rid,
        actual_micro=900_000,
        settled_usage_type="Credits",
        success=True,
    )

    assert result["outcome"] == SettleOutcome.SETTLED
    assert len(claim_sql) == 1
    assert "tr_settle_outbox" in claim_sql[0]
    assert db.reservations[rid]["terminal_at"] is not None


def test_absent_availability_cache_can_flip_to_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NotFound(Exception):
        pass

    table_present = False
    probe_calls = 0
    guarded_claims = 0

    def changing_availability(sql: str) -> None:
        nonlocal probe_calls, guarded_claims
        if sql == GUARD_COUNT_SQL:
            probe_calls += 1
            if not table_present:
                raise NotFound("Table not found: tr_settle_outbox")
        if (
            "UPDATE tr_reservation SET settled=true" in sql
            and "tr_settle_outbox" in sql
        ):
            guarded_claims += 1
        return None

    # Expire the negative entry immediately to model the normal short-TTL
    # recheck without making the test sleep.
    monkeypatch.setattr(
        "trusted_router.storage_gcp_authorize._OUTBOX_ABSENT_CACHE_SECONDS",
        0.0,
    )
    store, _db, _ = make_fake_store()
    before_ddl = _expired_authorization(store, ws="ws_before_outbox_ddl")
    after_ddl = _expired_authorization(store, ws="ws_after_outbox_ddl")
    proxied = _ProxyDatabase(store._database, changing_availability)
    proxied.name = "projects/p/instances/i/databases/ddl-flip"

    first = settle_atomic(
        proxied,
        store._param_types,
        reservation_id=before_ddl["reservation_id"],
        actual_micro=900_000,
        settled_usage_type="Credits",
        success=True,
    )
    assert first["outcome"] == SettleOutcome.SETTLED
    assert guarded_claims == 0

    table_present = True
    second = settle_atomic(
        proxied,
        store._param_types,
        reservation_id=after_ddl["reservation_id"],
        actual_micro=900_000,
        settled_usage_type="Credits",
        success=True,
    )
    assert second["outcome"] == SettleOutcome.SETTLED
    assert probe_calls == 2
    assert guarded_claims == 1


def test_completion_helpers_use_unguarded_sql_when_outbox_table_missing() -> None:
    class NotFound(Exception):
        pass

    def missing_table(sql: str) -> None:
        if "tr_settle_outbox" in sql:
            raise NotFound("Table not found: tr_settle_outbox")
        return None

    store, db, _ = make_fake_store()
    auth = _expired_authorization(store, ws="ws_missing_completion_helpers")
    rid, aid = auth["reservation_id"], auth["authorization_id"]
    db.reservations[rid]["settled"] = True
    db.reservations[rid]["terminal_at"] = None
    db.gateway_authorizations[aid] = {
        "authorization_id": aid,
        "settled": True,
        "terminal_at": None,
    }
    proxied = _ProxyDatabase(store._database, missing_table)

    def complete(transaction: Any) -> None:
        assert complete_reservation_retention(
            transaction,
            store._param_types,
            rid,
            terminal_at=_NOW,
            outbox_available=False,
        ) == 1
        assert complete_gateway_authorization_retention(
            transaction,
            store._param_types,
            aid,
            terminal_at=_NOW,
            outbox_available=False,
        ) == 1

    proxied.run_in_transaction(complete)

    assert db.reservations[rid]["terminal_at"] == _NOW
    assert db.gateway_authorizations[aid]["terminal_at"] == _NOW


def test_typed_finalize_threads_missing_outbox_signal_to_completion() -> None:
    class NotFound(Exception):
        pass

    def missing_table(sql: str) -> None:
        if "tr_settle_outbox" in sql:
            raise NotFound("Table not found: tr_settle_outbox")
        return None

    store, db, _ = make_fake_store()
    auth = _expired_authorization(store, ws="ws_missing_finalize_completion")
    rid, aid = auth["reservation_id"], auth["authorization_id"]
    proxied = _ProxyDatabase(store._database, missing_table)

    result = typed_finalize_atomic(
        proxied,
        store._param_types,
        reservation_id=rid,
        authorization_id=aid,
        success=True,
        actual_micro=900_000,
        settled_usage_type="Credits",
        now=_NOW,
        auth_body_settled=f'{{"id":"{aid}","settled":true}}',
    )

    assert result["outcome"] == SettleOutcome.SETTLED
    assert db.reservations[rid]["settled"] is True
    assert db.reservations[rid]["terminal_at"] == _NOW


def test_probe_transient_notfound_fails_closed() -> None:
    class NotFound(Exception):
        pass

    def transient(sql: str) -> None:
        if sql == GUARD_COUNT_SQL:
            raise NotFound("Session not found")
        return None

    store, db, _ = make_fake_store()
    ws = "ws_guard_transient_notfound"
    auth = _expired_authorization(store, ws=ws)
    rid = auth["reservation_id"]

    proxied = _ProxyDatabase(store._database, transient)

    with pytest.raises(NotFound):
        reap_expired_reservations(proxied, store._param_types, now=_NOW)
    _assert_frozen(db, ws, rid)


def test_probe_wrapped_transient_naming_table_fails_closed() -> None:
    class NotFound(Exception):
        pass

    def wrapped_transient(sql: str) -> None:
        if sql == GUARD_COUNT_SQL:
            raise NotFound("Session not found while querying tr_settle_outbox")
        return None

    store, db, _ = make_fake_store()
    ws = "ws_guard_wrapped_transient"
    auth = _expired_authorization(store, ws=ws)
    rid = auth["reservation_id"]

    proxied = _ProxyDatabase(store._database, wrapped_transient)

    with pytest.raises(NotFound):
        reap_expired_reservations(proxied, store._param_types, now=_NOW)
    _assert_frozen(db, ws, rid)


def test_probe_does_not_exist_shapes_fail_closed() -> None:
    class NotFound(Exception):
        pass

    for i, message in enumerate((
        "Table tr_settle_outbox does not exist",
        'column "status" of relation "tr_settle_outbox" does not exist',
    )):

        def missing_table(sql: str, message: str = message) -> None:
            if sql == GUARD_COUNT_SQL:
                raise NotFound(message)
            return None

        store, db, _ = make_fake_store()
        ws = f"ws_guard_does_not_exist_{i}"
        auth = _expired_authorization(store, ws=ws)
        rid = auth["reservation_id"]

        proxied = _ProxyDatabase(store._database, missing_table)

        with pytest.raises(NotFound):
            reap_expired_reservations(proxied, store._param_types, now=_NOW)
        _assert_frozen(db, ws, rid)


def test_guarded_rows_do_not_starve_unguarded() -> None:
    store, db, _ = make_fake_store()
    ws_a = "ws_guard_starve_a"
    auth_a = _expired_authorization(store, ws=ws_a)
    rid_a, aid_a = auth_a["reservation_id"], auth_a["authorization_id"]
    _outbox(store).enqueue(_row(aid_a, rid_a))

    ws_b = "ws_guard_starve_b"
    auth_b = _expired_authorization(store, ws=ws_b)
    rid_b = auth_b["reservation_id"]

    # The sandwich guarantees a guarded row precedes the unguarded one in scan
    # order regardless of which side a future edit moves the setup blocks to
    # (round-1 starvation repro requires a guarded row ahead of the unguarded victim).
    ws_c = "ws_guard_starve_c"
    auth_c = _expired_authorization(store, ws=ws_c)
    rid_c, aid_c = auth_c["reservation_id"], auth_c["authorization_id"]
    _outbox(store).enqueue(_row(aid_c, rid_c))

    assert reap_expired_reservations(store._database, store._param_types, now=_NOW, limit=1) == 1
    _assert_free_released(db, ws_b, rid_b)
    _assert_frozen(db, ws_a, rid_a)
    _assert_frozen(db, ws_c, rid_c)

    assert reap_expired_reservations(store._database, store._param_types, now=_NOW, limit=1) == 0
    _assert_frozen(db, ws_a, rid_a)
    _assert_frozen(db, ws_c, rid_c)


def test_availability_cache_works_for_unhashable_database() -> None:
    """The production google.cloud.spanner_v1.database.Database defines __eq__ and
    sets __hash__ = None. A cache keyed on the database OBJECT therefore raised
    TypeError on every lookup, silently never cached, and made every settle pay an
    extra GUARD_COUNT_SQL probe round trip on the hot path — invisible to tests
    whose fake database happens to be hashable. Key on a stable string instead.
    """

    class UnhashableNamedProxyDatabase(_ProxyDatabase):
        # Mirror the production type exactly: comparable, UNHASHABLE, and
        # carrying the fully-qualified database path the cache keys on.
        name = "projects/p/instances/i/databases/d"

        def __eq__(self, other: object) -> bool:
            return self is other

        __hash__ = None  # type: ignore[assignment]

    probe_calls = 0

    def count_probes(sql: str) -> None:
        nonlocal probe_calls
        if sql == GUARD_COUNT_SQL:
            probe_calls += 1
        return None

    store, _db, _ = make_fake_store()
    first_auth = _expired_authorization(store, ws="ws_unhashable_first")
    second_auth = _expired_authorization(store, ws="ws_unhashable_second")
    proxied = UnhashableNamedProxyDatabase(store._database, count_probes)
    assert proxied.__hash__ is None

    for authorization in (first_auth, second_auth):
        result = settle_atomic(
            proxied,
            store._param_types,
            reservation_id=authorization["reservation_id"],
            actual_micro=900_000,
            settled_usage_type="Credits",
            success=True,
        )
        assert result["outcome"] == SettleOutcome.SETTLED

    # One probe for the whole process, not one per settle.
    assert probe_calls == 1


def test_nameless_database_is_not_cached_by_reusable_id() -> None:
    """A database without a stable name is deliberately NOT cached: id() would be
    the obvious key, but addresses are reused after GC, so a destroyed double's
    entry could be inherited by an unrelated database and select stale SQL (a
    present-then-absent flip would emit guarded DML against a missing table).
    Re-probing an in-memory double is strictly cheaper than that risk."""
    probe_calls = 0

    def count_probes(sql: str) -> None:
        nonlocal probe_calls
        if sql == GUARD_COUNT_SQL:
            probe_calls += 1
        return None

    store, _db, _ = make_fake_store()
    first = _expired_authorization(store, ws="ws_nameless_first")
    second = _expired_authorization(store, ws="ws_nameless_second")
    proxied = _ProxyDatabase(store._database, count_probes)
    assert getattr(proxied, "name", None) is None

    for authorization in (first, second):
        result = settle_atomic(
            proxied,
            store._param_types,
            reservation_id=authorization["reservation_id"],
            actual_micro=900_000,
            settled_usage_type="Credits",
            success=True,
        )
        assert result["outcome"] == SettleOutcome.SETTLED

    # Probed each time rather than trusting a reusable-id cache entry.
    assert probe_calls == 2
