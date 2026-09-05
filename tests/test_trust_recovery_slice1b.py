from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path
from typing import Any

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.fakes.spanner import _FakeTransaction, make_fake_store
from trusted_router.services import trust_recovery
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_gcp_counter_dml import release_credit
from trusted_router.storage_models import AdverseTrustEvent, CreditProvenance

NOW = dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)
ROOT = Path(__file__).parents[1]


def _adverse(
    *,
    adverse_ref: str = "re_1",
    kind: str = "refund",
    status: str = "succeeded",
    amount: int = 600,
    watermark: str = "00000000000000000001:evt_1",
    provider: str = "stripe",
) -> AdverseTrustEvent:
    return AdverseTrustEvent(
        event_id=f"evt:{adverse_ref}",
        provider=provider,
        kind=kind,
        adverse_ref=adverse_ref,
        original_payment_ref="pi_recovery",
        amount_micro=amount,
        provider_subtype=f"charge.{kind}.updated",
        lifecycle_status=status,
        occurred_at=NOW,
        provider_ordering_watermark=watermark,
        payload="{}",
    )


def _store_with_payment(
    *, credited: int = 1_000, charged: int = 1_200
) -> tuple[Any, Any, str]:
    store, database, _ = make_fake_store()
    workspace = store.create_workspace("owner", "recovery", trial_credit_microdollars=0)
    assert store.credit_workspace_typed_direct(
        workspace.id,
        credited,
        "evt_payment",
        provenance=CreditProvenance("checkout", "stripe", "pi_recovery", NOW),
        payment_amount_microdollars=charged,
        currency="usd",
    )
    return store, database, workspace.id


def _payment(database: Any, workspace_id: str) -> dict[str, Any]:
    return database.typed["tr_trust_event"][(workspace_id, "evt_payment")]


def _balance(database: Any, workspace_id: str) -> int:
    return sum(
        int(row["total_credits"])
        for (owner, _shard), row in database.typed["tr_credit_balance"].items()
        if owner == workspace_id
    )


def test_refund_recovers_principal_pro_rata_not_fee_inclusive_charge() -> None:
    store, database, workspace_id = _store_with_payment()

    result = store.record_adverse_trust_event(_adverse())

    assert result.outcome == "applied"
    assert result.recovery_target == 500
    assert (result.recovered_micro, result.unrecovered_micro) == (500, 0)
    assert _balance(database, workspace_id) == 500
    payment = _payment(database, workspace_id)
    assert payment["recovery_target"] == 500
    assert payment["recovery_target"] == (
        payment["recovered_micro"] + payment["unrecovered_micro"]
    )
    shards = [
        row
        for (owner, _), row in database.typed["tr_credit_balance"].items()
        if owner == workspace_id
    ]
    assert all(row["trust_latched_at"] is not None for row in shards)
    assert all(row["billing_pause_causes"] == [] for row in shards)


def test_partial_refunds_are_order_independent_and_replay_is_a_noop() -> None:
    store, database, workspace_id = _store_with_payment(credited=900, charged=1_000)
    first = _adverse(adverse_ref="re_a", amount=200, watermark="01:a")
    second = _adverse(adverse_ref="re_b", amount=300, watermark="02:b")

    assert store.record_adverse_trust_event(second).recovery_target == 270
    assert store.record_adverse_trust_event(first).recovery_target == 450
    before = _balance(database, workspace_id)
    replay = store.record_adverse_trust_event(first)

    assert replay.outcome == "replay"
    assert _balance(database, workspace_id) == before == 450
    assert _payment(database, workspace_id)["cumulative_refunded"] == 500


def test_dispute_then_refund_reversal_keeps_full_claim_until_dispute_won() -> None:
    store, database, workspace_id = _store_with_payment()
    refund = _adverse(amount=600, watermark="01:refund")
    dispute = _adverse(
        adverse_ref="dp_1",
        kind="dispute",
        amount=1_200,
        watermark="02:dispute",
    )

    assert store.record_adverse_trust_event(refund).recovery_target == 500
    assert store.record_adverse_trust_event(dispute).recovery_target == 1_000
    reversed_refund = _adverse(
        status="reversed", amount=600, watermark="03:refund-reversed"
    )
    assert store.record_adverse_trust_event(reversed_refund).recovery_target == 1_000
    won = _adverse(
        adverse_ref="dp_1",
        kind="dispute",
        status="won",
        amount=1_200,
        watermark="04:won",
    )
    result = store.record_adverse_trust_event(won)

    assert result.recovery_target == 0
    assert (result.recovered_micro, result.unrecovered_micro) == (0, 0)
    assert _balance(database, workspace_id) == 1_000


def test_multi_shard_debit_takes_maximum_safe_and_pauses_for_remainder() -> None:
    store, database, workspace_id = _store_with_payment()
    shards = {
        shard: row
        for (owner, shard), row in database.typed["tr_credit_balance"].items()
        if owner == workspace_id
    }
    for row in shards.values():
        row["total_usage"] = row["total_credits"]
    shards[0]["total_usage"] -= 100
    shards[1]["total_usage"] -= 75

    result = store.record_adverse_trust_event(
        _adverse(kind="dispute", adverse_ref="dp_partial", amount=1_200)
    )

    assert (result.recovery_target, result.recovered_micro, result.unrecovered_micro) == (
        1_000,
        175,
        825,
    )
    committed_shards = [
        row
        for (owner, _), row in database.typed["tr_credit_balance"].items()
        if owner == workspace_id
    ]
    assert all(
        row["billing_pause_causes"] == ["principal_recovery"]
        and row["pause_epoch"] == 1
        for row in committed_shards
    )
    assert store.get_workspace(workspace_id).billing_paused is True


def test_later_topup_absorbs_oldest_debt_and_clears_only_recovery_cause() -> None:
    store, database, workspace_id = _store_with_payment(credited=1_000, charged=1_000)
    for (owner, _), row in database.typed["tr_credit_balance"].items():
        if owner == workspace_id:
            row["total_usage"] = row["total_credits"]
            row["billing_pause_causes"] = ["migration"]
    workspace = store.get_workspace(workspace_id)
    workspace.billing_pause_causes = ["migration"]
    workspace.billing_paused = True
    store._write_entity("workspace", workspace_id, workspace)
    result = store.record_adverse_trust_event(
        _adverse(kind="dispute", adverse_ref="dp_debt", amount=1_000)
    )
    assert result.unrecovered_micro == 1_000

    assert store.credit_workspace_typed_direct(
        workspace_id,
        1_200,
        "evt_later_payment",
        provenance=CreditProvenance("checkout", "stripe", "pi_later", NOW),
        payment_amount_microdollars=1_200_000,
        currency="usd",
    )

    assert _payment(database, workspace_id)["unrecovered_micro"] == 0
    assert _balance(database, workspace_id) == 1_200
    assert all(
        row["billing_pause_causes"] == ["migration"]
        for (owner, _), row in database.typed["tr_credit_balance"].items()
        if owner == workspace_id
    )
    assert store.get_workspace(workspace_id).billing_pause_causes == ["migration"]


def test_inbox_before_payment_drains_in_payment_transaction() -> None:
    store, database, _ = make_fake_store()
    workspace = store.create_workspace("owner", "inbox", trial_credit_microdollars=0)
    adverse = _adverse(amount=600)
    assert store.record_adverse_trust_event(adverse).outcome == "inbox"
    assert ("stripe", "re_1") in database.typed["tr_trust_inbox"]

    assert store.credit_workspace_typed_direct(
        workspace.id,
        1_000_000,
        "evt_payment",
        provenance=CreditProvenance("checkout", "stripe", "pi_recovery", NOW),
        payment_amount_microdollars=1_200_000,
        currency="usd",
    )

    assert ("stripe", "re_1") not in database.typed["tr_trust_inbox"]
    assert _payment(database, workspace.id)["recovery_target"] == 500
    payment_version = database.typed_versions[
        ("tr_trust_event", (workspace.id, "evt_payment"))
    ]
    assert database.typed_versions[
        ("tr_trust_event", (workspace.id, adverse.event_id))
    ] == payment_version


def test_stale_and_illegal_transitions_apply_no_money() -> None:
    store, database, workspace_id = _store_with_payment()
    assert store.record_adverse_trust_event(_adverse(watermark="10:new")).outcome == "applied"
    before = (_balance(database, workspace_id), dict(_payment(database, workspace_id)))

    stale = store.record_adverse_trust_event(
        _adverse(status="reversed", watermark="09:old")
    )
    illegal = store.record_adverse_trust_event(
        _adverse(status="pending", watermark="11:later")
    )

    assert (stale.outcome, illegal.outcome) == ("stale", "illegal")
    assert (_balance(database, workspace_id), _payment(database, workspace_id)) == before


def test_concurrent_replay_debits_once() -> None:
    store, database, workspace_id = _store_with_payment()
    database._ready_barrier = threading.Barrier(2)
    results: list[str] = []

    def apply() -> None:
        results.append(store.record_adverse_trust_event(_adverse()).outcome)

    first = threading.Thread(target=apply)
    second = threading.Thread(target=apply)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    database._ready_barrier = None

    assert sorted(results) == ["applied", "replay"]
    assert _balance(database, workspace_id) == 500


_SHARD_SET_SQL = "SELECT shard FROM tr_credit_balance WHERE workspace_id=@pk ORDER BY shard"


def _transaction_sql_spy(monkeypatch: Any) -> list[str]:
    seen: list[str] = []
    original = _FakeTransaction.execute_sql

    def spy(self: Any, sql: str, **kwargs: Any) -> Any:
        seen.append(sql)
        return original(self, sql, **kwargs)

    monkeypatch.setattr(_FakeTransaction, "execute_sql", spy)
    return seen


def test_release_without_payment_debt_never_reads_the_shard_set(monkeypatch: Any) -> None:
    """F4 (2026-09-05 convoy): the all-shard read takes ReaderShared on every
    credit shard while the settle holds Exclusive on one of them. A workspace
    with no payment claim — the common case — must not pay for it."""
    store, database, _ = make_fake_store()
    workspace = store.create_workspace("owner", "no-debt", trial_credit_microdollars=1_000)
    database.typed["tr_credit_balance"][(workspace.id, 0)]["reserved"] = 50
    seen = _transaction_sql_spy(monkeypatch)

    count = database.run_in_transaction(
        lambda transaction: release_credit(
            transaction,
            store._param_types,
            workspace.id,
            50,
            0,
            shard=0,
        )
    )

    assert count == 1
    assert database.typed["tr_credit_balance"][(workspace.id, 0)]["reserved"] == 0
    assert any("FROM tr_trust_event" in sql for sql in seen), "debt indicator read still runs"
    assert _SHARD_SET_SQL not in seen


def test_release_with_open_claim_reads_the_shard_set_and_absorbs(monkeypatch: Any) -> None:
    store, database, workspace_id = _store_with_payment(credited=1_000, charged=1_000)
    for (owner, _), row in database.typed["tr_credit_balance"].items():
        if owner == workspace_id:
            row["total_usage"] = row["total_credits"]
    shard = database.typed["tr_credit_balance"][(workspace_id, 0)]
    shard["reserved"] = 50
    shard["total_usage"] = shard["total_credits"] - 50
    result = store.record_adverse_trust_event(
        _adverse(kind="dispute", adverse_ref="dp_lazy_release", amount=1_000)
    )
    assert result.unrecovered_micro == 1_000
    seen = _transaction_sql_spy(monkeypatch)

    count = database.run_in_transaction(
        lambda transaction: release_credit(
            transaction,
            store._param_types,
            workspace_id,
            50,
            0,
            shard=0,
        )
    )

    assert count == 1
    assert _SHARD_SET_SQL in seen
    assert _payment(database, workspace_id)["unrecovered_micro"] == 950
    assert _payment(database, workspace_id)["recovered_micro"] == 50


def test_reservation_release_satisfies_open_recovery_claim() -> None:
    store, database, workspace_id = _store_with_payment(credited=1_000, charged=1_000)
    for (owner, _), row in database.typed["tr_credit_balance"].items():
        if owner == workspace_id:
            row["total_usage"] = row["total_credits"]
    shard = database.typed["tr_credit_balance"][(workspace_id, 0)]
    shard["reserved"] = 50
    shard["total_usage"] = shard["total_credits"] - 50
    result = store.record_adverse_trust_event(
        _adverse(kind="dispute", adverse_ref="dp_release", amount=1_000)
    )
    assert result.unrecovered_micro == 1_000

    count = database.run_in_transaction(
        lambda transaction: release_credit(
            transaction,
            store._param_types,
            workspace_id,
            50,
            0,
            shard=0,
        )
    )

    assert count == 1
    assert _payment(database, workspace_id)["unrecovered_micro"] == 950
    assert _payment(database, workspace_id)["recovered_micro"] == 50


def test_inbox_schema_and_every_conflict_clause_have_explicit_targets() -> None:
    ddl = (ROOT / "scripts/deploy/migrate_typed_counters.sh").read_text()
    postgres = (ROOT / "src/trusted_router/storage_postgres_schema.sql").read_text()
    source = (ROOT / "src/trusted_router/storage_postgres.py").read_text()
    assert "CREATE TABLE tr_trust_inbox" in ddl
    assert "CREATE TABLE IF NOT EXISTS tr_trust_inbox" in postgres
    assert "PRIMARY KEY (provider, adverse_ref)" in ddl
    assert "PRIMARY KEY (provider, adverse_ref)" in postgres
    assert "ON CONFLICT DO NOTHING" not in source
    assert "ON CONFLICT (provider, adverse_ref) DO NOTHING" in source
    assert "ON CONFLICT (provider, adverse_ref, kind) DO NOTHING" in source


def test_stripe_refund_webhook_records_recovery_before_ack(client: Any) -> None:
    workspace = STORE.create_workspace("owner", "stripe webhook", trial_credit_microdollars=0)
    assert STORE.credit_workspace_typed_direct(
        workspace.id,
        1_000_000,
        "evt_route_payment",
        provenance=CreditProvenance("checkout", "stripe", "pi_route", NOW),
        payment_amount_microdollars=1_200_000,
        currency="usd",
    )

    response = client.post(
        "/v1/internal/stripe/webhook",
        json={
            "id": "evt_route_refund",
            "created": int(NOW.timestamp()),
            "type": "refund.updated",
            "data": {
                "object": {
                    "id": "re_route",
                    "payment_intent": "pi_route",
                    "amount": 60,
                    "status": "succeeded",
                }
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["adverse"] == [
        {
            "adverse_ref": "re_route",
            "provider": "stripe",
            "kind": "refund",
            "status": "succeeded",
            "outcome": "applied",
            "workspace_id": workspace.id,
            "recovery_target": 500_000,
            "unrecovered_micro": 0,
        }
    ]
    assert STORE.credit_money_snapshot(workspace.id) == (500_000, 0, 0)


def test_x402_refund_uses_same_webhook_handler_under_x402_provider(client: Any) -> None:
    workspace = STORE.create_workspace("owner", "x402 webhook", trial_credit_microdollars=0)
    assert STORE.credit_workspace_typed_direct(
        workspace.id,
        1_000_000,
        "evt_x402_route_payment",
        provenance=CreditProvenance("x402", "x402", "pi_x402_route", NOW),
        payment_amount_microdollars=1_000_000,
        currency="usd",
    )

    response = client.post(
        "/v1/internal/stripe/webhook",
        json={
            "id": "evt_x402_route_refund",
            "created": int(NOW.timestamp()),
            "type": "refund.created",
            "data": {
                "object": {
                    "id": "re_x402_route",
                    "payment_intent": "pi_x402_route",
                    "amount": 25,
                    "status": "succeeded",
                    "metadata": {"payment_method": "x402"},
                }
            },
        },
    )

    assert response.status_code == 200
    adverse = response.json()["data"]["adverse"][0]
    assert (adverse["provider"], adverse["recovery_target"], adverse["outcome"]) == (
        "x402",
        250_000,
        "applied",
    )
    assert STORE.credit_money_snapshot(workspace.id) == (750_000, 0, 0)


def test_stripe_dispute_webhook_claims_all_then_won_restores(client: Any) -> None:
    workspace = STORE.create_workspace("owner", "dispute webhook", trial_credit_microdollars=0)
    assert STORE.credit_workspace_typed_direct(
        workspace.id,
        1_000_000,
        "evt_dispute_payment",
        provenance=CreditProvenance("checkout", "stripe", "pi_dispute_route", NOW),
        payment_amount_microdollars=1_200_000,
        currency="usd",
    )
    body = {
        "id": "evt_dispute_created",
        "created": int(NOW.timestamp()),
        "type": "charge.dispute.created",
        "data": {
            "object": {
                "id": "dp_route",
                "payment_intent": "pi_dispute_route",
                "amount": 120,
                "status": "needs_response",
            }
        },
    }
    created = client.post("/v1/internal/stripe/webhook", json=body)
    assert created.status_code == 200
    assert created.json()["data"]["adverse"][0]["recovery_target"] == 1_000_000
    assert STORE.credit_money_snapshot(workspace.id) == (0, 0, 0)

    body["id"] = "evt_dispute_won"
    body["created"] += 1
    body["type"] = "charge.dispute.closed"
    body["data"]["object"]["status"] = "won"
    won = client.post("/v1/internal/stripe/webhook", json=body)

    assert won.status_code == 200
    assert won.json()["data"]["adverse"][0]["recovery_target"] == 0
    assert STORE.credit_money_snapshot(workspace.id) == (1_000_000, 0, 0)


def test_inbox_sweeper_alerts_after_provider_consistency_delay(monkeypatch: Any) -> None:
    store = InMemoryStore()
    assert store.record_adverse_trust_event(_adverse()).outcome == "inbox"
    row = store.trust_inbox[("stripe", "re_1")]
    store.trust_inbox[("stripe", "re_1")] = type(row)(
        row.provider,
        row.adverse_ref,
        row.payload,
        NOW - dt.timedelta(minutes=16),
    )
    calls: list[tuple[str, list[str], dict[str, str]]] = []
    monkeypatch.setattr(
        trust_recovery,
        "ops_alert",
        lambda message, *, fingerprint, tags: calls.append(
            (message, fingerprint, tags)
        ),
    )

    assert trust_recovery.alert_stale_trust_inbox(store, now=NOW) == 1
    assert calls[0][0].startswith("trust.inbox_stale provider=stripe adverse_ref=re_1")


def test_postgres_adverse_recovery_uses_explicit_dedup_key() -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    workspace = store.create_workspace(
        "owner", "postgres recovery", trial_credit_microdollars=0
    )
    assert store.credit_workspace_typed_direct(
        workspace.id,
        1_000,
        "evt_pg_payment",
        provenance=CreditProvenance("checkout", "stripe", "pi_recovery", NOW),
        payment_amount_microdollars=1_200,
        currency="usd",
    )

    applied = store.record_adverse_trust_event(_adverse())
    replay = store.record_adverse_trust_event(_adverse())

    assert (applied.outcome, applied.recovery_target) == ("applied", 500)
    assert replay.outcome == "replay"
    balance = conn.execute(
        "SELECT SUM(total_credits) FROM tr_credit_balance WHERE workspace_id = %s",
        (workspace.id,),
    ).fetchone()
    assert balance == (500,)
