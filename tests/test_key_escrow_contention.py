"""Preserve escrow decisions while avoiding futile and overbroad writes."""

from __future__ import annotations

import copy
import random
from typing import Any

import pytest

from tests.fakes.spanner import FakeSpannerDatabase, _FakeTransaction, _ParamTypes
from trusted_router.storage_gcp_counters import KEY_LIMIT_TABLE, distribute_credit_amount
from trusted_router.storage_gcp_key_escrow import rebalance_key_limit_headroom


def _seed(limits: list[int], consumed: list[int]) -> FakeSpannerDatabase:
    db = FakeSpannerDatabase()
    db.typed[KEY_LIMIT_TABLE] = {
        ("key", shard): {
            "key_hash": "key", "shard": shard, "limit_micro": limit,
            "usage": consumed[shard], "byok_usage": 0, "reserved": 0,
            "include_byok": True, "day_usage": 17,
        }
        for shard, limit in enumerate(limits)
    }
    return db


def _repair(db: Any, count: int, required: int, target: int = 0) -> bool:
    return rebalance_key_limit_headroom(
        db, _ParamTypes, key_hash="key", shard_count=count,
        estimate=required, preferred_shard=target,
    )


def test_exhaustion_never_enters_write_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _seed([50] * 16, [49] * 16)
    before = copy.deepcopy(db.typed)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("exhausted escrow must use only a snapshot")

    monkeypatch.setattr(db, "run_in_transaction", forbidden)
    assert not _repair(db, 16, 17)
    assert db.typed == before


def test_only_changed_allowances_use_update(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _seed([5, 3, 4, 4], [0] * 4)
    calls: list[dict[str, Any]] = []
    update = _FakeTransaction.update

    def track(self: Any, **kwargs: Any) -> None:
        calls.append(kwargs)
        update(self, **kwargs)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("escrow repair must not upsert existing rows")

    monkeypatch.setattr(_FakeTransaction, "update", track)
    monkeypatch.setattr(_FakeTransaction, "insert_or_update", forbidden)
    assert _repair(db, 4, 4)
    assert calls == [{
        "table": KEY_LIMIT_TABLE, "columns": ("key_hash", "shard", "limit_micro"),
        "values": [("key", 0, 7), ("key", 2, 3), ("key", 3, 3)],
    }]
    calls.clear()
    assert _repair(db, 4, 4)
    assert calls == []
    assert all(row["day_usage"] == 17 for row in db.typed[KEY_LIMIT_TABLE].values())


def test_snapshot_cannot_authorize_a_stale_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _seed([50] * 4, [0] * 4)
    run = db.run_in_transaction

    def peer_spends_first(*args: Any, **kwargs: Any) -> Any:
        for row in db.typed[KEY_LIMIT_TABLE].values():
            row["reserved"] = 50
        return run(*args, **kwargs)

    monkeypatch.setattr(db, "run_in_transaction", peer_spends_first)
    assert not _repair(db, 4, 60)
    assert all(row["limit_micro"] == 50 for row in db.typed[KEY_LIMIT_TABLE].values())


@pytest.mark.parametrize("include_byok", [False, True])
def test_repair_matches_original_allocation_differential(include_byok: bool) -> None:
    rng = random.Random(20261015)  # noqa: S311 - deterministic differential inputs
    for case in range(500):
        count = rng.choice([2, 4, 16])
        limits = [rng.randrange(100) + (150 if case % 2 else 0) for _ in range(count)]
        usages = [rng.randrange(100) for _ in range(count)]
        db = _seed(limits, usages)
        consumed = []
        for row in db.typed[KEY_LIMIT_TABLE].values():
            row.update(include_byok=include_byok, byok_usage=rng.randrange(20), reserved=rng.randrange(20))
            consumed.append(row["usage"] + row["reserved"] + (row["byok_usage"] if include_byok else 0))
        before = copy.deepcopy(db.typed[KEY_LIMIT_TABLE])
        required, target = rng.randrange(1, 100), rng.randrange(count)
        remaining = sum(limits) - sum(consumed)
        assert _repair(db, count, required, target) == (remaining >= required)
        expected = copy.deepcopy(before)
        if remaining >= required:
            extras = list(distribute_credit_amount(remaining - required, count))
            extras[target] += required
            for shard in range(count):
                expected[("key", shard)]["limit_micro"] = consumed[shard] + extras[shard]
        assert db.typed[KEY_LIMIT_TABLE] == expected
        assert sum(row["limit_micro"] for row in expected.values()) == sum(limits)


def test_update_mutation_cannot_create_missing_allowance() -> None:
    from google.api_core.exceptions import NotFound

    db = _seed([50, 50], [0, 0])
    before = copy.deepcopy(db.typed)
    with pytest.raises(NotFound):
        db.run_in_transaction(lambda tx: tx.update(
            table=KEY_LIMIT_TABLE, columns=("key_hash", "shard", "limit_micro"),
            values=[("key", 0, 25), ("key", 2, 75)],
        ))
    assert db.typed == before
