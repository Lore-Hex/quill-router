"""Native C1 acceptance. PROFILE output counts are not physical scan counts.

The emulator exposes rows_returned and DML row_count_exact, but no optimizer
plan or rows_scanned, so these tests check results: which rows each guarded
statement changes, and what the finalize batch commits.
"""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from google.cloud.spanner_v1 import KeySet
from google.cloud.spanner_v1 import param_types as pt
from google.cloud.spanner_v1.transaction import Transaction
from google.cloud.spanner_v1.types import ExecuteSqlRequest

from tests.conformance.spanner_ddl import DDL
from tests.conformance.spanner_sql_builders import NOW, Capture
from tests.conformance.test_spanner_sql_acceptance import execute_dml, rolled_back
from trusted_router import storage_gcp_authorize as finalize
from trusted_router import storage_gcp_counter_dml as counters
from trusted_router.spend_windows import window_floors
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_operational_analytics_outbox import (
    SpannerOperationalAnalyticsOutbox,
)
from trusted_router.storage_gcp_request_records import gateway_authorization_insert_statement
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
from trusted_router.storage_gcp_trust import TRUST_EVENT_COLUMNS, absorb_unrecovered_recovery_tx
from trusted_router.storage_models import GatewayAuthorization, Generation, SettleOutboxRow
from trusted_router.types import UsageType

pytestmark = pytest.mark.xdist_group("conformance-spanner-emulator")


@pytest.fixture(params=["spanner-emulator"], ids=lambda backend: f"backend={backend}")
def c1_database(request, native_emulator_resources):
    assert request.param == "spanner-emulator"
    schema = tuple(DDL)
    # The SDK Database exposes its Instance only as `_instance` (no public accessor).
    database = native_emulator_resources[0]._instance.database(  # noqa: SLF001 - SDK has no public accessor
        "c1-" + uuid4().hex[:12], ddl_statements=schema[:20],
    )
    database.create().result(timeout=120)
    try:
        for offset in range(20, len(schema), 20):
            database.update_ddl(schema[offset:offset + 20]).result(timeout=120)
        yield database
    finally:
        database.close()
        database.drop()


def recovery_select(workspace):
    capture = Capture()
    absorb_unrecovered_recovery_tx(
        capture, pt, workspace_id=workspace, amount_micro=30, shard_count=1,
        now=NOW, read_entity_tx=None, write_entity_tx=None,
    )
    assert len(capture.statements) == 1
    return capture.statements[0]


def profile(transaction, statement):
    sql, params, types = statement
    assert "FORCE_INDEX" not in sql
    result = transaction.execute_sql(sql, params=params, param_types=types,
                                     query_mode=ExecuteSqlRequest.QueryMode.PROFILE)
    rows = list(result)
    assert result.stats is not None
    assert int(result.stats.query_stats["rows_returned"]) == len(rows)
    return rows, result.stats


def test_payment_debt_guard_and_recovery_select(c1_database):
    database = c1_database
    workspace = "debt-" + uuid4().hex
    columns = ("workspace_id", "event_id", "kind", "provider", "occurred_at",
               "recorded_at", "unrecovered_micro", "recovered_micro")
    for offset in range(0, 5000, 500):
        with database.batch() as batch:
            batch.insert("tr_trust_event", columns=columns, values=[
                (workspace, f"recovered-{i:05}", "payment", "stripe", NOW, NOW, 0, 100)
                for i in range(offset, offset + 500)
            ])
    with database.batch() as batch:
        batch.insert("tr_credit_balance", columns=("workspace_id", "shard", "reserved"),
                     values=[(workspace, 0, 100)])
    with database.snapshot() as snapshot:
        assert list(snapshot.execute_sql(
            "SELECT COUNT(*) FROM tr_trust_event WHERE workspace_id=@ws",
            params={"ws": workspace}, param_types={"ws": pt.STRING},
        )) == [[5000]]
    select = recovery_select(workspace)
    credit = counters.release_credit_no_debt_statement(pt, workspace, 100, 70, shard=0)
    for debt in (0, 50):
        if debt:
            with database.batch() as batch:
                batch.insert("tr_trust_event", columns=columns, values=[
                    (workspace, "positive", "payment", "stripe", NOW, NOW, debt, 0),
                ])
        with rolled_back(database) as transaction:
            rows, _ = profile(transaction, select)
            assert len(rows) == bool(debt)
            if debt:
                assert rows[0][TRUST_EVENT_COLUMNS.index("event_id")] == "positive"
                assert rows[0][TRUST_EVENT_COLUMNS.index("unrecovered_micro")] == 50
            _, stats = profile(transaction, credit)
            assert stats.row_count_exact == (0 if debt else 1)
            # Explicitly assert PROFILE's timing-independent result counts;
            # never relabel rows_returned as rows_scanned.
            assert list(transaction.execute_sql(
                "SELECT reserved, total_usage FROM tr_credit_balance WHERE workspace_id=@ws AND shard=0",
                params={"ws": workspace}, param_types={"ws": pt.STRING},
            )) == ([[100, 0]] if debt else [[0, 70]])


@pytest.mark.parametrize("flow", ["two_commit", "one_commit"])
@pytest.mark.parametrize("window", ["current", "rollover", "null-starts"])
def test_finalize_ten_statement_native_readback(c1_database, monkeypatch, window, flow):
    database = c1_database
    monkeypatch.setattr(finalize, "utcnow", lambda: NOW)
    identity = uuid4().hex
    ws, key, aid, rid = (prefix + identity for prefix in ("ws-", "key-", "auth-", "res-"))
    floors = window_floors(NOW)
    starts = [floors[name] for name in ("daily", "weekly", "monthly")]
    if window == "rollover":
        starts = [start - timedelta(days=40) for start in starts]
    elif window == "null-starts":
        starts = [None, None, None]
    # Beyond exact IEEE-754 integers, but safely within INT64. Distinct window
    # values catch cross-wired/doubled arithmetic without a SQLite type oracle.
    base = 2**53 + 1
    with database.batch() as batch:
        batch.insert("tr_credit_balance", columns=("workspace_id", "shard", "total_credits", "reserved", "total_usage"),
                     values=[(ws, 3, 1000, 100, base)])
        batch.insert("tr_key_limit", columns=("key_hash", "shard", "reserved", "usage", "byok_usage",
                                              "day_usage", "week_usage", "month_usage", "day_start", "week_start", "month_start"),
                     values=[(key, 2, 100, base, 23, 25, 35, 45, *starts)])
    authorization = GatewayAuthorization(
        id=aid, workspace_id=ws, key_hash=key, model_id="model", provider="provider",
        usage_type=UsageType.CREDITS, estimated_microdollars=100, credit_reservation_id=rid,
    )
    seed = [gateway_authorization_insert_statement(pt, authorization, created_at=NOW),
            counters.reservation_insert_statement(
                pt, reservation_id=rid, workspace_id=ws, key_hash=key, ws_shard=0,
                credit_shard=3, key_shard=2, credit_reserved_micro=100, key_reserved_micro=100,
                hold_usage_type="Credits", authorization_id=aid, idempotency_scope=identity,
                idempotency_fingerprint=identity, expires_at=NOW + timedelta(hours=1), created_at=NOW,
            )]
    database.run_in_transaction(lambda tx: execute_dml(tx, seed, batch=True))
    intent = SettleOutboxRow(
        authorization_id=aid, reservation_id=rid, intent_kind="settle", settle_origin="typed", actual_cost_micro=70,
    )
    if flow == "two_commit":
        SpannerSettleOutbox(database, pt).enqueue(intent)
    generation = Generation.from_settle_body(
        authorization=authorization, provider_name="provider", model_id="model", usage_type="Credits",
        provider="provider", body={}, input_tokens=5, output_tokens=7, actual_cost_microdollars=70,
    )
    authorization.record_finalization(success=True, actual_microdollars=70,
                                      selected_usage_type="Credits", generation=generation)
    batches = []
    original = Transaction.batch_update

    def record_batch(self, statements, *args, **kwargs):
        status, counts = original(self, statements, *args, **kwargs)
        batches.append((statements, status.code, counts))
        return status, counts

    monkeypatch.setattr(Transaction, "batch_update", record_batch)
    result = finalize.typed_finalize_atomic(
        database, pt, reservation_id=rid, authorization_id=aid, success=True, actual_micro=70,
        settled_usage_type="Credits", now=NOW, outbox_available=True, authorization=authorization,
        auth_body_settled=json_body(authorization), generation=generation, persist_generation_record=True,
        operational_analytics_outbox=SpannerOperationalAnalyticsOutbox(database, pt),
        **({"settle_outbox_done": (aid, "settle")} if flow == "two_commit"
           else {"settle_outbox_intent": intent, "intent_initial_delay_seconds": 60}),
    )
    assert result["outcome"] == "settled" and result["outbox_marked"] is True
    # Every window state settles in the one batch: the key's two forms are
    # complementary, so a stale or never-set window rolls forward in place.
    assert result["attempts"] == 1
    [(statements, code, counts)] = batches
    # The two-commit finalize has ten statements; the one-commit settle adds
    # the intent's INSERT and its two retention clears. Either way the counter
    # releases are last: the credit, then the key's two window forms.
    assert len(statements) == (10 if flow == "two_commit" else 12)
    current = int(window == "current")
    assert code == 0 and counts[-3:] == [1, current, 1 - current]
    if flow == "two_commit":
        assert counts == [1] * 8 + [current, 1 - current]
    assert "UPDATE tr_credit_balance" in statements[-3][0]
    assert "UPDATE tr_key_limit" in statements[-2][0] and "UPDATE tr_key_limit" in statements[-1][0]
    # A fresh snapshot after commit proves rejected batch writes were rolled
    # back: the fallback releases and charges exactly once even after rollover.
    with database.snapshot(multi_use=True) as snapshot:
        credit = list(snapshot.read("tr_credit_balance", columns=("reserved", "total_usage", "total_credits"),
                                    keyset=KeySet(keys=[[ws, 3]])))
        assert credit == [[0, base + 70, 1000]]
        key_rows = list(snapshot.read("tr_key_limit", columns=("reserved", "usage", "byok_usage",
                                      "day_usage", "week_usage", "month_usage", "day_start", "week_start", "month_start"),
                                      keyset=KeySet(keys=[[key, 2]])))
        expected_usage = [95, 105, 115] if window == "current" else [70, 70, 70]
        assert key_rows == [[0, base + 70, 23, *expected_usage,
                             *(floors[name] for name in ("daily", "weekly", "monthly"))]]
        assert list(snapshot.read("tr_reservation", columns=("settled", "actual_micro"),
                                  keyset=KeySet(keys=[[rid]]))) == [[True, 70]]
        assert list(snapshot.read("tr_gateway_authorization", columns=("settled",),
                                  keyset=KeySet(keys=[[aid]]))) == [[True]]
        assert list(snapshot.read("tr_settle_outbox", columns=("status",),
                                  keyset=KeySet(keys=[[aid, "settle"]]))) == [["done"]]
