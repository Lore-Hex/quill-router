"""Sharded exact key caps: bounded per-transaction candidates and a lock-free
headroom precheck before any escrow move or denial.

Key usage rows are already sharded (``ApiKey.usage_shard_count``, escrowed
lifetime caps, exact release by the recorded ``key_shard``). These tests pin the
authorize side the same way credit is pinned: one transaction tries at most
``MAX_KEY_SHARD_ATTEMPTS_PER_TRANSACTION`` randomized shards; a refusal is then
classified on a lock-free snapshot of every shard: retry the one funded shard,
move pooled escrow (read-write), or deny with no write lock at all.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from typing import Any

import pytest

from tests.fakes.spanner import FakeSpannerDatabase, _FakeSnapshot, make_fake_store
from tests.fakes.spanner_order import (
    authorize_credit_before_key,
    credit_before_key,
    record_statements,
    transaction_statements,
)
from trusted_router import storage_gcp, storage_gcp_key_escrow
from trusted_router.storage_gcp_authorize import (
    EXHAUSTED,
    HEADROOM,
    MAX_KEY_SHARD_ATTEMPTS_PER_TRANSACTION,
    AuthorizeOutcome,
    authorize_atomic,
    bounded_key_shard_candidates,
    key_lifetime_cap_precheck,
)
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE
from trusted_router.storage_gcp_key_escrow import KeyHeadroom, key_headroom_precheck
from trusted_router.storage_models import CreditAccount

WS = "ws-key-shards"


def _store(*, key_shards: int, limit: int | None, credit: int = 10_000_000) -> tuple[Any, Any, Any]:
    store, db = make_fake_store(request_record_write_mode="typed")
    store._write_entity("credit", WS, CreditAccount(workspace_id=WS))
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(WS, 0)] = {
        "workspace_id": WS, "shard": 0, "total_credits": credit, "total_usage": 0,
        "reserved": 0, "source_updated_at": None, "updated_at": None,
    }
    _raw, key = store.api_keys.create(
        workspace_id=WS, name="sharded", creator_user_id=None, limit_microdollars=limit,
        usage_shard_count=key_shards,
    )
    rows = db.typed[KEY_LIMIT_TABLE]
    assert sorted(shard for kh, shard in rows if kh == key.hash) == list(range(key_shards))
    if limit is not None:
        assert sum(rows[(key.hash, s)]["limit_micro"] for s in range(key_shards)) == limit
    return store, db, key


def _authorize(store: Any, key: Any, estimate: int, **kwargs: Any) -> tuple[str, Any]:
    options = dict(
        workspace_id=WS, key_hash=key.hash, estimate=estimate, has_credit_candidate=True,
        reservation_usage_type="Credits", model_id="model", provider="provider",
        requested_model_id=None, candidate_model_ids=["model"], region="us",
        endpoint_id="endpoint", candidate_endpoint_ids=["endpoint"], idempotency_key=None,
        idempotency_fingerprint=None, key_usage_shards=key.usage_shard_count,
        expires_at="2099-01-01T00:00:00Z",
    )
    options.update(kwargs)
    return store.authorize_gateway_typed(**options)


def _settle(store: Any, authorization: Any, actual: int) -> None:
    assert store.typed_finalize_gateway_authorization_result(
        authorization.id, success=True, actual_microdollars=actual,
        selected_usage_type="Credits",
    ).finalized


def _key_rows(db: Any, key: Any) -> list[dict[str, Any]]:
    rows = db.typed[KEY_LIMIT_TABLE]
    return [rows[(key.hash, shard)] for shard in range(key.usage_shard_count)]


@pytest.fixture
def rotating_shards(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Deterministic 'random' shard orders: request i starts at shard i mod N."""
    counter = itertools.count()

    def rotate(count: int) -> tuple[int, ...]:
        if count == 1:
            return (0,)
        first = next(counter) % count
        return tuple((first + offset) % count for offset in range(count))

    monkeypatch.setattr(storage_gcp, "randomized_credit_shards", rotate)
    yield


@pytest.fixture
def no_escrow_rebalance(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    def refuse(*_args: Any, **_kwargs: Any) -> bool:
        calls.append(1)
        raise AssertionError("the escrow rebalance must not run")

    monkeypatch.setattr(storage_gcp_key_escrow, "rebalance_key_limit_headroom", refuse)
    return calls


@pytest.mark.usefixtures("rotating_shards")
@pytest.mark.parametrize("key_shards", [1, 4])
def test_reserve_spreads_release_is_exact_and_replay_keeps_the_shard(key_shards: int) -> None:
    store, db, key = _store(key_shards=key_shards, limit=8_000)
    holds = []
    for index in range(8):
        verdict, authorization = _authorize(
            store, key, 1_000, idempotency_key=f"spread-{index}", idempotency_fingerprint="same",
        )
        assert verdict == AuthorizeOutcome.ACCEPTED
        holds.append(authorization)
    shards = [db.reservations[a.credit_reservation_id]["key_shard"] for a in holds]
    # Every hold is recorded on the shard that took it; N=4 spreads evenly.
    assert sorted(shards) == sorted(list(range(key_shards)) * (8 // key_shards))
    assert [row["reserved"] for row in _key_rows(db, key)] == [8_000 // key_shards] * key_shards

    # The cap is global and exact: no ninth hold fits on any shard or the pool.
    verdict, refused = _authorize(store, key, 1_000, idempotency_key="spread-over")
    assert verdict == AuthorizeOutcome.KEY_LIMIT_EXCEEDED and refused is None

    # Idempotent replay returns the original authorization and key shard.
    replay_verdict, replayed = _authorize(
        store, key, 1_000, idempotency_key="spread-0", idempotency_fingerprint="same",
    )
    assert replay_verdict == AuthorizeOutcome.REPLAY and replayed.id == holds[0].id
    assert db.reservations[holds[0].credit_reservation_id]["key_shard"] == shards[0]

    for index, authorization in enumerate(holds):
        _settle(store, authorization, 100 + index)
    booked = [0] * key_shards
    for index, shard in enumerate(shards):
        booked[shard] += 100 + index
    rows = _key_rows(db, key)
    # Release is exact per recorded shard; usage and the lazy windows book
    # onto that same row.
    assert [row["reserved"] for row in rows] == [0] * key_shards
    assert [row["usage"] for row in rows] == booked
    assert [row["day_usage"] for row in rows] == booked
    usage = store.typed_key_usage(key.hash)
    assert usage["usage"] == sum(booked) and usage["reserved"] == 0
    assert usage["windows"]["daily"] == sum(booked)


def test_a_refusal_locks_at_most_the_bounded_prefix_and_needs_no_rebalance(
    monkeypatch: pytest.MonkeyPatch, no_escrow_rebalance: list[int],
) -> None:
    """Before this bound a refusal of a 16-shard key tried all 16 key rows in one
    transaction (each refused UPDATE plus its classification read keeps its row
    lock until rollback) while that transaction held the workspace's credit row,
    then always ran the read-write escrow rebalance to prove exhaustion."""
    from tests.fakes.spanner import _FakeTransaction

    store, db, key = _store(key_shards=16, limit=16_000)
    for row in _key_rows(db, key):
        row["usage"] = row["limit_micro"]  # every sub-budget spent
    calls = record_statements(monkeypatch)
    reserved_shards: list[int] = []
    original = _FakeTransaction.execute_update

    def spy(tx: Any, sql: str, **kwargs: Any) -> int:
        if sql.startswith("UPDATE tr_key_limit SET reserved = reserved + @est"):
            reserved_shards.append(int(kwargs["params"]["shard"]))
        return original(tx, sql, **kwargs)

    monkeypatch.setattr(_FakeTransaction, "execute_update", spy)

    verdict, authorization = _authorize(store, key, 1_000)

    assert verdict == AuthorizeOutcome.KEY_LIMIT_EXCEEDED and authorization is None
    # The speculative shard, then one sequential pass over the bounded prefix.
    assert 1 <= len(set(reserved_shards)) <= MAX_KEY_SHARD_ATTEMPTS_PER_TRANSACTION
    assert len(reserved_shards) == 1 + MAX_KEY_SHARD_ATTEMPTS_PER_TRANSACTION
    for tx in dict.fromkeys(tx for tx, _ in calls):
        statements = transaction_statements([call for call in calls if call[0] is tx])
        credit_before_key(statements, require_both=False)
    assert no_escrow_rebalance == []  # the lock-free snapshot proved exhaustion
    assert key.hash in store._lifetime_cap_exhausted_keys
    assert sum(row["reserved"] for row in _key_rows(db, key)) == 0


@pytest.mark.usefixtures("no_escrow_rebalance")
def test_funded_shard_beyond_the_prefix_is_retried_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    store, db, key = _store(key_shards=8, limit=8_000)
    monkeypatch.setattr(storage_gcp, "randomized_credit_shards", lambda count: tuple(range(count)))
    rows = _key_rows(db, key)
    for row in rows[:7]:
        row["usage"] = row["limit_micro"]  # only shard 7 keeps its escrow
    attempts: list[tuple[int, ...]] = []
    import trusted_router.storage_gcp_authorize as authorize_module

    real_atomic = authorize_module.authorize_atomic

    def spy(*args: Any, **kwargs: Any) -> Any:
        attempts.append(tuple(kwargs["key_shard_candidates"]))
        return real_atomic(*args, **kwargs)

    monkeypatch.setattr(authorize_module, "authorize_atomic", spy)
    verdict, authorization = _authorize(store, key, 1_000)
    assert verdict == AuthorizeOutcome.ACCEPTED
    assert attempts == [(0, 1, 2, 3), (7,)]
    assert db.reservations[authorization.credit_reservation_id]["key_shard"] == 7
    # Commits replace row dicts in the fake; read the committed rows again.
    assert [row["reserved"] for row in _key_rows(db, key)] == [0] * 7 + [1_000]
    assert key.hash not in store._lifetime_cap_exhausted_keys


@pytest.mark.parametrize("key_shards", [4, 8])
def test_fragmented_pool_moves_escrow_then_accepts(key_shards: int) -> None:
    store, db, key = _store(key_shards=key_shards, limit=8_000)
    estimate = 8_000 // key_shards + 1  # more than any one sub-budget
    verdict, authorization = _authorize(store, key, estimate)
    assert verdict == AuthorizeOutcome.ACCEPTED
    rows = _key_rows(db, key)
    assert sum(row["limit_micro"] for row in rows) == 8_000  # escrow moved, not minted
    assert sum(row["reserved"] for row in rows) == estimate
    assert rows[db.reservations[authorization.credit_reservation_id]["key_shard"]][
        "reserved"
    ] == estimate


@pytest.mark.parametrize("key_shards", [1, 4])
def test_exhausted_pool_is_denied_without_a_write_lock(
    key_shards: int, no_escrow_rebalance: list[int],
) -> None:
    store, db, key = _store(key_shards=key_shards, limit=4_000)
    for row in _key_rows(db, key):
        row["usage"] = row["limit_micro"] - 100  # 100 left per shard
    commits = db.commits
    estimate = 100 * key_shards + 1  # the pool cannot cover it
    verdict, authorization = _authorize(store, key, estimate)
    assert verdict == AuthorizeOutcome.KEY_LIMIT_EXCEEDED and authorization is None
    assert no_escrow_rebalance == []
    assert db.commits == commits  # every attempt rolled back; nothing committed
    assert key.hash in store._lifetime_cap_exhausted_keys
    # The negative cache's lock-free precheck now refuses without a transaction.
    tags = len(db.transaction_tags)
    verdict, _ = _authorize(store, key, estimate)
    assert verdict == AuthorizeOutcome.KEY_LIMIT_EXCEEDED
    assert len(db.transaction_tags) == tags
    # ...and a request the pool does cover still gets through.
    verdict, authorization = _authorize(store, key, 100)
    assert verdict == AuthorizeOutcome.ACCEPTED


@pytest.mark.parametrize("key_shards", [1, 4])
def test_lifetime_cap_precheck_sums_every_shard(key_shards: int) -> None:
    store, db, key = _store(key_shards=key_shards, limit=4_000)
    for row in _key_rows(db, key):
        row["usage"] = row["limit_micro"] - 250
    pooled = 250 * key_shards

    def verdict(estimate: int) -> str:
        return key_lifetime_cap_precheck(
            db, store._param_types, key_hash=key.hash, estimate=estimate,
            has_credit_candidate=True, shard_count=key_shards,
        )

    assert verdict(250) == HEADROOM
    assert verdict(pooled) == HEADROOM
    assert verdict(pooled + 1) == EXHAUSTED


@pytest.mark.parametrize("key_shards", [1, 4])
def test_authorize_takes_credit_before_key_with_sharded_keys(
    monkeypatch: pytest.MonkeyPatch, key_shards: int,
) -> None:
    store, _db, key = _store(key_shards=key_shards, limit=8_000)
    calls = record_statements(monkeypatch)
    verdict, _authorization = _authorize(store, key, 1_000)
    assert verdict == AuthorizeOutcome.ACCEPTED
    authorize_credit_before_key(transaction_statements(calls))


def _precheck(db: Any, key: Any, estimate: int, *, has_credit_candidate: bool = True) -> KeyHeadroom:
    return key_headroom_precheck(
        db, key_shards_types(), key_hash=key.hash, shard_count=key.usage_shard_count,
        estimate=estimate, has_credit_candidate=has_credit_candidate,
    )


def key_shards_types() -> Any:
    from tests.fakes.spanner import _ParamTypes

    return _ParamTypes


def test_key_headroom_precheck_classifies_a_refusal() -> None:
    store, db, key = _store(key_shards=4, limit=4_000)
    rows = _key_rows(db, key)
    for row, used in zip(rows, (1_000, 900, 600, 1_000), strict=True):
        row["usage"] = used  # headroom 0, 100, 400, 0
    assert _precheck(db, key, 400) == KeyHeadroom(decisive=True, funded_shard=2, aggregate_covers=True)
    assert _precheck(db, key, 500) == KeyHeadroom(decisive=True, funded_shard=None, aggregate_covers=True)
    assert _precheck(db, key, 501) == KeyHeadroom(decisive=True, funded_shard=None, aggregate_covers=False)
    # An overdrawn shard counts against the pool exactly as the rebalance proves it.
    rows[0]["usage"] = 1_200
    assert _precheck(db, key, 400) == KeyHeadroom(decisive=True, funded_shard=2, aggregate_covers=False)


@pytest.mark.parametrize("shape", ["incomplete", "uncapped", "mixed_byok", "byok_excluded", "unreadable"])
def test_key_headroom_precheck_defers_when_rows_prove_nothing(
    monkeypatch: pytest.MonkeyPatch, shape: str,
) -> None:
    store, db, key = _store(key_shards=4, limit=4_000)
    rows = _key_rows(db, key)
    has_credit_candidate = True
    if shape == "incomplete":
        del db.typed[KEY_LIMIT_TABLE][(key.hash, 3)]
    elif shape == "uncapped":
        rows[1]["limit_micro"] = None
    elif shape == "mixed_byok":
        rows[2]["include_byok"] = False
    elif shape == "byok_excluded":
        for row in rows:
            row["include_byok"] = False
        has_credit_candidate = False
    else:
        def unreadable(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("snapshot unavailable")

        monkeypatch.setattr(FakeSpannerDatabase, "snapshot", unreadable)
    assert _precheck(db, key, 1, has_credit_candidate=has_credit_candidate) == KeyHeadroom(
        decisive=False,
    )
    del store


def test_one_transaction_never_takes_more_than_the_bound() -> None:
    assert bounded_key_shard_candidates(tuple(range(16))) == (0, 1, 2, 3)
    with pytest.raises(ValueError, match="must not be empty"):
        bounded_key_shard_candidates(())
    _unused, db, key = _store(key_shards=8, limit=8_000)
    with pytest.raises(ValueError, match="hot-path transaction limit"):
        authorize_atomic(
            db, key_shards_types(), workspace_id=WS, key_hash=key.hash, estimate=1,
            has_credit_candidate=True, reservation_usage_type="Credits",
            idempotency_scope=None, idempotency_fingerprint=None,
            expires_at="2099-01-01T00:00:00Z", build_auth_body=lambda a, r: "{}",
            key_shard_candidates=tuple(range(5)),
        )


def test_precheck_reads_on_a_snapshot_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The precheck is lock-free: one snapshot read of every configured shard."""
    store, db, key = _store(key_shards=4, limit=4_000)
    reads: list[str] = []
    original = _FakeSnapshot.execute_sql

    def spy(snapshot: Any, sql: str, **kwargs: Any) -> Any:
        reads.append(" ".join(sql.split()))
        return original(snapshot, sql, **kwargs)

    monkeypatch.setattr(_FakeSnapshot, "execute_sql", spy)
    commits, tags = db.commits, len(db.transaction_tags)
    assert _precheck(db, key, 1).decisive
    assert db.commits == commits and len(db.transaction_tags) == tags
    assert reads == [
        "SELECT shard, limit_micro, usage, byok_usage, reserved, include_byok FROM tr_key_limit "
        "WHERE key_hash=@kh AND shard>=0 AND shard<@shard_count ORDER BY shard"
    ]
    del store
