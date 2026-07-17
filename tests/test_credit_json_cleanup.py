from __future__ import annotations

import json
from typing import Any

import pytest

from scripts import cleanup_legacy_credit_json
from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_gcp_credit_json_cleanup import (
    LEGACY_CREDIT_MONEY_FIELDS,
    cleanup_credit_json,
    inspect_credit_json,
    legacy_credit_workspace_ids,
)


def _seed_credit(
    store: Any,
    db: Any,
    workspace_id: str,
    *,
    shard_count: int = 1,
    with_legacy_money: bool = True,
    complete_typed: bool = True,
) -> None:
    body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "shard_count": shard_count,
        "stripe_customer_id": "cus_keep",
        "stripe_payment_method_id": "pm_keep",
        "auto_refill_enabled": True,
        "future_metadata": {"preserve": True},
    }
    if with_legacy_money:
        body.update(
            {
                "total_credits_microdollars": 10_000_000,
                "total_usage_microdollars": 2_000_000,
                "reserved_microdollars": 300_000,
            }
        )
    store._write_entity("credit", workspace_id, body)
    typed = db.typed.setdefault(CREDIT_BALANCE_TABLE, {})
    count = shard_count if complete_typed else max(0, shard_count - 1)
    for shard in range(count):
        typed[(workspace_id, shard)] = {
            "workspace_id": workspace_id,
            "shard": shard,
            "total_credits": 5_000_000,
            "total_usage": 1_000_000,
            "reserved": 0,
            "source_updated_at": None,
            "updated_at": None,
        }


def _raw_credit(db: Any, workspace_id: str) -> dict[str, Any]:
    return json.loads(db.rows[("credit", workspace_id)].body)


def test_cleanup_dry_run_reports_fields_without_mutating() -> None:
    store, db, _ = make_fake_store()
    _seed_credit(store, db, "ws_dry")
    before = db.rows[("credit", "ws_dry")].body

    result = cleanup_credit_json(store, "ws_dry")

    assert result.ready
    assert not result.applied
    assert set(result.legacy_fields) == LEGACY_CREDIT_MONEY_FIELDS
    assert db.rows[("credit", "ws_dry")].body == before


def test_cleanup_apply_strips_only_retired_money_fields() -> None:
    store, db, _ = make_fake_store()
    _seed_credit(store, db, "ws_apply")
    typed_before = dict(db.typed[CREDIT_BALANCE_TABLE][("ws_apply", 0)])

    result = cleanup_credit_json(store, "ws_apply", apply=True)

    assert result.ready and result.applied
    body = _raw_credit(db, "ws_apply")
    assert not LEGACY_CREDIT_MONEY_FIELDS.intersection(body)
    assert body["stripe_customer_id"] == "cus_keep"
    assert body["stripe_payment_method_id"] == "pm_keep"
    assert body["auto_refill_enabled"] is True
    assert body["future_metadata"] == {"preserve": True}
    assert db.typed[CREDIT_BALANCE_TABLE][("ws_apply", 0)] == typed_before


def test_cleanup_is_idempotent_for_an_already_clean_row() -> None:
    store, db, _ = make_fake_store()
    _seed_credit(store, db, "ws_clean", with_legacy_money=False)
    before = db.rows[("credit", "ws_clean")].body

    result = cleanup_credit_json(store, "ws_clean", apply=True)

    assert result.ready
    assert not result.needs_cleanup
    assert not result.applied
    assert db.rows[("credit", "ws_clean")].body == before


def test_cleanup_accepts_complete_sharded_ledger() -> None:
    store, db, _ = make_fake_store()
    _seed_credit(store, db, "ws_sharded", shard_count=3)

    result = cleanup_credit_json(store, "ws_sharded", apply=True)

    assert result.ready and result.applied
    assert result.expected_shards == (0, 1, 2)
    assert result.observed_shards == (0, 1, 2)


def test_cleanup_refuses_incomplete_typed_shard_set() -> None:
    store, db, _ = make_fake_store()
    _seed_credit(store, db, "ws_incomplete", shard_count=3, complete_typed=False)
    before = db.rows[("credit", "ws_incomplete")].body

    result = cleanup_credit_json(store, "ws_incomplete", apply=True)

    assert not result.ready
    assert not result.applied
    assert result.reason == "authoritative typed shard set is incomplete"
    assert result.expected_shards == (0, 1, 2)
    assert result.observed_shards == (0, 1)
    assert db.rows[("credit", "ws_incomplete")].body == before


def test_inspection_rejects_row_id_body_mismatch() -> None:
    store, db, _ = make_fake_store()
    _seed_credit(store, db, "ws_mismatch")
    body = _raw_credit(db, "ws_mismatch")
    body["workspace_id"] = "different"
    store._write_entity("credit", "ws_mismatch", body)

    result = inspect_credit_json(store, "ws_mismatch")

    assert not result.ready
    assert result.reason == "credit metadata workspace_id does not match row id"


def test_legacy_workspace_scan_returns_only_stale_rows() -> None:
    store, db, _ = make_fake_store()
    _seed_credit(store, db, "ws_stale")
    _seed_credit(store, db, "ws_clean", with_legacy_money=False)

    assert legacy_credit_workspace_ids(store) == ["ws_stale"]


def test_legacy_workspace_scan_uses_row_id_not_untrusted_body_id() -> None:
    store, db, _ = make_fake_store()
    _seed_credit(store, db, "ws_row_id")
    body = _raw_credit(db, "ws_row_id")
    body["workspace_id"] = "wrong-body-id"
    store._write_entity("credit", "ws_row_id", body)

    assert legacy_credit_workspace_ids(store) == ["ws_row_id"]


def test_cli_preflights_every_row_before_applying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-bigtable")
    store, db, _ = make_fake_store()
    _seed_credit(store, db, "a_ready")
    _seed_credit(store, db, "z_blocked", shard_count=2, complete_typed=False)
    ready_before = db.rows[("credit", "a_ready")].body

    rc = cleanup_legacy_credit_json.main(["--apply"], store=store)

    assert rc == 1
    assert db.rows[("credit", "a_ready")].body == ready_before


def test_cli_apply_cleans_all_stale_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-bigtable")
    store, db, _ = make_fake_store()
    _seed_credit(store, db, "ws_one")
    _seed_credit(store, db, "ws_two", shard_count=2)

    rc = cleanup_legacy_credit_json.main(["--apply"], store=store)

    assert rc == 0
    assert legacy_credit_workspace_ids(store) == []
    assert not LEGACY_CREDIT_MONEY_FIELDS.intersection(_raw_credit(db, "ws_one"))
    assert not LEGACY_CREDIT_MONEY_FIELDS.intersection(_raw_credit(db, "ws_two"))
