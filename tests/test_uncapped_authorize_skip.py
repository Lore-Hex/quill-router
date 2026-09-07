"""Contention contract, observed at the fake Spanner statement/mutation boundary.

Reversion/mutation checks are documented in docs/storage-portability/uncapped-skip-validation.md.
"""
from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.fakes import spanner
from tests.test_gateway_authorize_spanner_operations import (
    _body,
    _request,
    _seed_typed_gateway_store,
)
from trusted_router import storage_gcp
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.spend_windows import utcnow, window_floors
from trusted_router.storage_gcp_authorize import SettleOutcome, settle_atomic
from trusted_router.storage_gcp_counters import KEY_LIMIT_TABLE

RESERVE_SQL = (
    "UPDATE tr_key_limit SET reserved = reserved + @est "
    "WHERE key_hash=@kh AND shard=@shard AND limit_micro IS NOT NULL "
    "AND (@is_byok = FALSE OR include_byok = TRUE) "
    "AND (limit_micro - usage - IF(include_byok, byok_usage, 0) - reserved) >= @est"
)


@pytest.fixture
def operations(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    log: list[tuple[str, str, dict[str, Any]]] = []

    def wrap(cls: Any, name: str) -> None:
        original = getattr(cls, name)

        def record(self: Any, *args: Any, **kwargs: Any) -> Any:
            target = str(args[0] if args else kwargs.get("table", ""))
            log.append((name, target, copy.deepcopy(kwargs)))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(cls, name, record)

    for cls in (spanner._FakeTransaction, spanner._FakeSnapshot):
        wrap(cls, "execute_sql")
    wrap(spanner._FakeTransaction, "execute_update")
    for cls in (spanner._FakeTransaction, spanner._FakeBatch):
        wrap(cls, "insert_or_update")
        wrap(cls, "delete")
    return log


def seed(monkeypatch: pytest.MonkeyPatch, *, cap: int | None = None,
         window: str | None = None, include_byok: bool = True,
         alert_only: bool = False) -> tuple[Any, Any, Any]:
    store, db, key = _seed_typed_gateway_store()
    key.limit_microdollars = cap
    key.include_byok_in_limit = include_byok
    key.usage_shard_count = 4
    key.budget_alert_only = alert_only
    if window:
        setattr(key, f"limit_{window}_microdollars", 50_000_000)
    store._write_entity("api_key", key.hash, key)
    base = db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]
    for shard in range(4):
        row = dict(base, shard=shard, limit_micro=cap, include_byok=include_byok)
        for period, floor in window_floors(utcnow()).items():
            row[{"daily": "day_start", "weekly": "week_start", "monthly": "month_start"}[period]] = floor
        db.typed[KEY_LIMIT_TABLE][(key.hash, shard)] = row
    monkeypatch.setattr(storage_gcp, "randomized_credit_shards",
                        lambda count: (3, 1, 2, 0) if count == 4 else (0,))
    return store, db, key


def authorize(key: Any, *, byok: bool = False, idem: str = "uncapped") -> dict[str, Any]:
    body = _body(key.hash, idempotency_key=idem)
    if byok:
        body = type(body).model_validate(dict(body.model_dump(), provider={"usage": "byok"}))
    return gateway._authorize_gateway_sync(_request(), body, Settings(environment="test"))["data"]


def key_ops(log: list[tuple[str, str, dict[str, Any]]]) -> list[Any]:
    return [op for op in log if KEY_LIMIT_TABLE in op[1]]


@pytest.mark.parametrize("include_byok", [False, True])
@pytest.mark.parametrize("alert_only", [False, True])
def test_uncapped_authorize_zero_counter_statements_or_mutations(
    monkeypatch: pytest.MonkeyPatch, operations: list[Any], include_byok: bool, alert_only: bool,
) -> None:
    _, db, key = seed(monkeypatch, include_byok=include_byok,
                      window="daily" if alert_only else None, alert_only=alert_only)
    operations.clear()
    data = authorize(key)
    assert key_ops(operations) == []
    res = db.reservations[data["credit_reservation_id"]]
    assert res["key_shard"] == 3
    assert res["key_reserved_micro"] == 0


@pytest.mark.parametrize("window", [None, "daily", "weekly", "monthly"])
@pytest.mark.parametrize("byok,include_byok", [(False, True), (True, False), (True, True)])
def test_capped_and_window_paths_keep_exact_reserve_sql_and_parameters(
    monkeypatch: pytest.MonkeyPatch, operations: list[Any], window: str | None,
    byok: bool, include_byok: bool,
) -> None:
    cap = 50_000_000 if window is None else None
    store, db, key = seed(monkeypatch, cap=cap, window=window, include_byok=include_byok)
    if byok:
        store.upsert_byok_provider(workspace_id=key.workspace_id, provider="anthropic",
                                   secret_ref="env://ANTHROPIC_API_KEY", key_hint="test")  # noqa: S106
    operations.clear()
    data = authorize(key, byok=byok)
    res = db.reservations[data["credit_reservation_id"]]
    estimate = store.get_gateway_authorization(data["authorization_id"]).estimated_microdollars
    assert estimate > 0
    updates = [op for op in key_ops(operations) if op[0] == "execute_update"]
    if window is not None and byok and not include_byok:
        assert key_ops(operations) == []
        assert res["key_reserved_micro"] == 0
        return
    assert len(updates) == 1
    assert updates[0] == ("execute_update", RESERVE_SQL, {
        "params": {"est": estimate,
                   "kh": key.hash, "shard": 3, "is_byok": byok},
        "param_types": {"est": store._param_types.INT64, "kh": store._param_types.STRING,
                        "shard": store._param_types.INT64, "is_byok": store._param_types.BOOL},
    })
    assert res["key_reserved_micro"] == (
        estimate if cap is not None and (not byok or include_byok) else 0
    )
    reads = [op for op in key_ops(operations) if op[0] == "execute_sql"]
    assert bool(reads) == (window is not None or (byok and not include_byok))


def settle(store: Any, reservation_id: str, amount: int = 700) -> dict[str, Any]:
    return settle_atomic(store._database, store._param_types, reservation_id=reservation_id,
                         actual_micro=amount, settled_usage_type="Credits", success=True)


def test_uncapped_settle_books_one_selected_shard_and_display_sums(
    monkeypatch: pytest.MonkeyPatch, operations: list[Any],
) -> None:
    store, db, key = seed(monkeypatch)
    db.typed[KEY_LIMIT_TABLE][(key.hash, 1)]["usage"] = 123
    operations.clear()
    data = authorize(key)
    assert key_ops(operations) == []
    operations.clear()
    assert settle(store, data["credit_reservation_id"])["outcome"] == SettleOutcome.SETTLED
    updates = [op for op in key_ops(operations) if op[0] == "execute_update"]
    assert len(updates) == 1
    assert updates[0][2]["params"]["shard"] == 3
    assert updates[0][2]["params"]["hold"] == 0
    rows = db.typed[KEY_LIMIT_TABLE]
    assert [rows[(key.hash, i)]["usage"] for i in range(4)] == [0, 123, 0, 700]
    [display] = store.list_api_keys_with_usage(key.workspace_id)
    assert display.usage_microdollars == 823
    assert display.reserved_microdollars == 0
    assert display.windows == {"daily": 700, "weekly": 700, "monthly": 700}


def test_legacy_authorization_still_settles(monkeypatch: pytest.MonkeyPatch) -> None:
    store, db, key = seed(monkeypatch)
    # Old callers omit the opt-in, yielding the pre-change KEY_NO_HOLD record.
    _, auth = store.authorize_gateway_typed(
        workspace_id=key.workspace_id, key_hash=key.hash, estimate=1000,
        has_credit_candidate=True, reservation_usage_type="Credits", model_id="m",
        provider="anthropic", requested_model_id="m", candidate_model_ids=["m"],
        region=None, endpoint_id=None, candidate_endpoint_ids=[], idempotency_key=None,
        idempotency_fingerprint=None, key_usage_shards=4,
    )
    assert auth is not None
    rid = auth.credit_reservation_id
    assert db.reservations[rid]["key_reserved_micro"] == 0
    assert settle(store, rid)["outcome"] == SettleOutcome.SETTLED
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 3)]["usage"] == 700


def test_uncapped_replay_keeps_authorization_and_books_once(
    monkeypatch: pytest.MonkeyPatch, operations: list[Any],
) -> None:
    store, db, key = seed(monkeypatch)
    operations.clear()
    first = authorize(key)
    assert key_ops(operations) == []
    monkeypatch.setattr(storage_gcp, "randomized_credit_shards", lambda count: tuple(range(count)))
    replay = authorize(key)
    assert replay["authorization_id"] == first["authorization_id"]
    assert replay["credit_reservation_id"] == first["credit_reservation_id"]
    assert replay["idempotent_replay"] is True
    assert key_ops(operations) == []
    rid = first["credit_reservation_id"]
    assert db.reservations[rid]["key_shard"] == 3
    assert len(db.reservations) == 1
    assert settle(store, rid)["outcome"] == SettleOutcome.SETTLED
    operations.clear()
    assert settle(store, rid)["outcome"] == SettleOutcome.ALREADY_SETTLED
    assert key_ops(operations) == []
    assert sum(row["usage"] for row in db.typed[KEY_LIMIT_TABLE].values()) == 700


@pytest.mark.parametrize("include_byok", [False, True])
def test_uncapped_byok_skips_authorize_and_settles_byok_usage(
    monkeypatch: pytest.MonkeyPatch, operations: list[Any], include_byok: bool,
) -> None:
    store, db, key = seed(monkeypatch, include_byok=include_byok)
    store.upsert_byok_provider(
        workspace_id=key.workspace_id, provider="anthropic",
        secret_ref="env://ANTHROPIC_API_KEY", key_hint="test",  # noqa: S106
    )
    operations.clear()
    data = authorize(key, byok=True)
    assert key_ops(operations) == []
    rid = data["credit_reservation_id"]
    assert db.reservations[rid]["key_reserved_micro"] == 0
    assert db.reservations[rid]["credit_reserved_micro"] == 0
    result = settle_atomic(store._database, store._param_types, reservation_id=rid,
                           actual_micro=700, settled_usage_type="BYOK", success=True)
    assert result["outcome"] == SettleOutcome.SETTLED
    [display] = store.list_api_keys_with_usage(key.workspace_id)
    assert display.usage_microdollars == 0
    assert display.byok_usage_microdollars == 700
    assert display.windows == dict.fromkeys(("daily", "weekly", "monthly"),
                                            700 if include_byok else 0)


@pytest.mark.parametrize("window", ["daily", "weekly", "monthly"])
def test_uncapped_window_limit_still_blocks(
    monkeypatch: pytest.MonkeyPatch, operations: list[Any], window: str,
) -> None:
    store, db, key = seed(monkeypatch, window=window)
    setattr(key, f"limit_{window}_microdollars", 1)
    store._write_entity("api_key", key.hash, key)
    operations.clear()
    with pytest.raises(Exception) as raised:
        authorize(key)
    assert getattr(raised.value, "status_code", None) == 429
    assert key_ops(operations)
    assert not db.reservations


@pytest.mark.parametrize("byok", [False, True])
def test_shrink_after_entity_read_books_shard_zero(
    monkeypatch: pytest.MonkeyPatch, operations: list[Any],
    caplog: pytest.LogCaptureFixture, byok: bool,
) -> None:
    store, db, key = seed(monkeypatch)
    key.usage_shard_count = 10
    store._write_entity("api_key", key.hash, key)
    db.typed[KEY_LIMIT_TABLE][(key.hash, 9)] = dict(
        db.typed[KEY_LIMIT_TABLE][(key.hash, 0)], shard=9,
    )
    monkeypatch.setattr(storage_gcp, "randomized_credit_shards",
                        lambda count: (9, 0) if count == 10 else (0,))
    if byok:
        store.upsert_byok_provider(workspace_id=key.workspace_id, provider="anthropic",
                                   secret_ref="env://ANTHROPIC_API_KEY", key_hint="test")  # noqa: S106
    original = type(store).authorize_gateway_typed

    def shrink(self: Any, **kwargs: Any) -> Any:
        assert kwargs["key_usage_shards"] == 10
        db.typed[KEY_LIMIT_TABLE].pop((key.hash, 9))
        key.usage_shard_count = 4
        store._write_entity("api_key", key.hash, key)
        return original(self, **kwargs)

    monkeypatch.setattr(type(store), "authorize_gateway_typed", shrink)
    data = authorize(key, byok=byok)
    rid = data["credit_reservation_id"]
    assert db.reservations[rid]["key_shard"] == 9
    result = settle_atomic(db, store._param_types, reservation_id=rid, actual_micro=700,
                           settled_usage_type="BYOK" if byok else "Credits", success=True)
    assert result["outcome"] == SettleOutcome.SETTLED
    row = db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]
    assert row["byok_usage" if byok else "usage"] == 700
    assert row["day_usage"] == 700
    events = [r for r in caplog.records
              if getattr(r, "metric", None) == "key_usage_shard_fallback_total"]
    assert len(events) == 1
    assert events[0].value == 1
    assert key.hash in events[0].getMessage()
    assert "shard=9" in events[0].getMessage()
    assert settle(store, rid)["outcome"] == SettleOutcome.ALREADY_SETTLED
    assert row["byok_usage" if byok else "usage"] == 700


@pytest.mark.parametrize("selected_shard", [0, 3])
def test_zero_rows_authorizes_then_fails_loudly_and_can_retry(
    monkeypatch: pytest.MonkeyPatch, operations: list[Any], selected_shard: int,
) -> None:
    store, db, key = seed(monkeypatch)
    backup = copy.deepcopy(db.typed[KEY_LIMIT_TABLE][(key.hash, 0)])
    db.typed[KEY_LIMIT_TABLE].clear()
    monkeypatch.setattr(storage_gcp, "randomized_credit_shards",
                        lambda count: (selected_shard,) if count == 4 else (0,))
    operations.clear()
    data = authorize(key)  # Deliberate KEY_MISSING change: no 402 on the skip path.
    assert key_ops(operations) == []
    rid = data["credit_reservation_id"]
    before = copy.deepcopy(db.reservations[rid])
    with pytest.raises(RuntimeError, match="actual_micro=700") as raised:
        settle(store, rid)
    assert key.hash in str(raised.value)
    assert rid in str(raised.value)
    assert db.reservations[rid] == before  # failed claim rolls back, still retryable
    db.typed[KEY_LIMIT_TABLE][(key.hash, 0)] = backup
    assert settle(store, rid)["outcome"] == SettleOutcome.SETTLED
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]["usage"] == 700


def test_capped_zero_rows_still_rejects_at_authorize(monkeypatch: pytest.MonkeyPatch) -> None:
    _, db, key = seed(monkeypatch, cap=50_000_000)
    db.typed[KEY_LIMIT_TABLE].clear()
    with pytest.raises(Exception) as raised:
        authorize(key)
    assert getattr(raised.value, "status_code", None) == 402
    assert not db.reservations


def test_cap_commit_bounded_inflight_slip_and_fresh_removal(
    monkeypatch: pytest.MonkeyPatch, operations: list[Any],
) -> None:
    store, db, key = seed(monkeypatch)
    original = type(store).authorize_gateway_typed
    changed = False

    def commit_cap(self: Any, **kwargs: Any) -> Any:
        nonlocal changed
        if not changed:
            assert kwargs["skip_key_limit"] is True
            changed = True
            assert store.update_key(key.hash, {"limit_microdollars": 1}) is not None
            operations.clear()
        return original(self, **kwargs)

    monkeypatch.setattr(type(store), "authorize_gateway_typed", commit_cap)
    first = authorize(key, idem="inflight")
    assert key_ops(operations) == []
    auth = store.get_gateway_authorization(first["authorization_id"])
    assert auth.estimated_microdollars > 1
    res = db.reservations[first["credit_reservation_id"]]
    assert res["key_reserved_micro"] == 0
    assert res["credit_reserved_micro"] == auth.estimated_microdollars
    with pytest.raises(Exception) as raised:
        authorize(key, idem="after-cap")
    assert getattr(raised.value, "status_code", None) == 402
    assert len(db.reservations) == 1
    assert store.update_key(key.hash, {"limit_microdollars": None}) is not None
    operations.clear()
    after = authorize(key, idem="after-removal")
    assert after["authorization_id"] != first["authorization_id"]
    assert key_ops(operations) == []
    assert len(db.reservations) == 2


@pytest.mark.parametrize("byok", [False, True])
def test_missing_shard_settlement_matches_healthy_ledger(
    monkeypatch: pytest.MonkeyPatch, byok: bool,
) -> None:
    """Differential money check: only booking location may change after shrink."""
    from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE

    ledgers = []
    for missing in (False, True):
        store, db, key = seed(monkeypatch)
        if byok:
            store.upsert_byok_provider(
                workspace_id=key.workspace_id, provider="anthropic",
                secret_ref="env://ANTHROPIC_API_KEY", key_hint="test",  # noqa: S106
            )
        data = authorize(key, byok=byok)
        rid = data["credit_reservation_id"]
        if missing:
            db.typed[KEY_LIMIT_TABLE].pop((key.hash, 3))
        result = settle_atomic(
            db, store._param_types, reservation_id=rid, actual_micro=700,
            settled_usage_type="BYOK" if byok else "Credits", success=True,
        )
        assert result["outcome"] == SettleOutcome.SETTLED
        key_fields = ("usage", "byok_usage", "reserved", "day_usage", "week_usage", "month_usage")
        credit_fields = ("total_credits", "total_usage", "reserved")
        ledgers.append((
            {field: sum(row[field] for row in db.typed[KEY_LIMIT_TABLE].values())
             for field in key_fields},
            {field: sum(row[field] for row in db.typed[CREDIT_BALANCE_TABLE].values())
             for field in credit_fields},
            db.reservations[rid]["actual_micro"],
        ))
    assert ledgers[0] == ledgers[1]
