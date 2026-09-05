from __future__ import annotations

import ast
import dataclasses
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from trusted_router.storage_models import AdverseTrustEvent, TrustEvent
from trusted_router.storage_trust_reconciliation import (
    MARKER_COLUMNS,
    SpannerTrustReconciliationRepository,
)
from trusted_router.stripe_trust_history import StripeTrustScan, scan_stripe_responses
from trusted_router.trust_reconcile_job import (
    run_historical_backfill,
    run_recurring_reconciliation,
)
from trusted_router.trust_reconciliation import (
    BackfillMarker,
    CanonicalTrustRecord,
    MarkerRequirement,
    OutstandingAdverse,
    canonical_mapping,
    canonical_records_from_events,
    completed_marker_satisfies,
    outstanding_is_beyond_horizon,
    reconcile_canonical_mappings,
    reconciliation_tail_start,
)
from trusted_router.trust_tiers import trust_reconciliation_is_fresh

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)
HISTORY_START = NOW - timedelta(days=100)


def _payment_intent(
    *,
    payment_intent_id: str = "pi_fee",
    created: datetime = HISTORY_START + timedelta(days=1),
    workspace_id: str = "ws_1",
    x402: bool = False,
) -> dict[str, Any]:
    metadata = {
        "workspace_id": workspace_id,
        "payment_method": "x402" if x402 else "card",
        "credit_amount_microdollars": "1000000",
        "processing_fee_cents": "20",
        "charge_amount_cents": "120",
    }
    if x402:
        metadata["amount_microdollars"] = "1000000"
    return {
        "id": payment_intent_id,
        "object": "payment_intent",
        "status": "succeeded",
        "created": int(created.timestamp()),
        "amount": 120,
        "amount_received": 120,
        "currency": "usd",
        "metadata": metadata,
    }


def _refund(
    *,
    status: str = "succeeded",
    created: datetime = HISTORY_START + timedelta(days=2),
    watermark: str = "00000000000000000002:evt_refund",
    payment_intent_id: str = "pi_fee",
) -> dict[str, Any]:
    return {
        "id": "re_fee",
        "object": "refund",
        "payment_intent": payment_intent_id,
        "amount": 60,
        "status": status,
        "created": int(created.timestamp()),
        "_trust_ordering_watermark": watermark,
    }


def _dispute(
    *,
    status: str = "needs_response",
    created: datetime = HISTORY_START + timedelta(days=3),
    payment_intent_id: str = "pi_fee",
) -> dict[str, Any]:
    return {
        "id": "dp_fee",
        "object": "dispute",
        "payment_intent": payment_intent_id,
        "amount": 120,
        "status": status,
        "created": int(created.timestamp()),
        "evidence_details": {"due_by": int((created + timedelta(days=7)).timestamp())},
        "_trust_ordering_watermark": "00000000000000000002:evt_dispute",
    }


class _Repository:
    def __init__(self) -> None:
        self.events: dict[tuple[str, str], TrustEvent] = {}
        self.markers: dict[tuple[str, str, str, str, str], BackfillMarker] = {}

    @staticmethod
    def _marker_key(values: tuple[str, str, str, str, str]) -> tuple[str, ...]:
        return values

    def get_marker(
        self,
        provider: str,
        account_id: str,
        environment: str,
        source: str,
        source_version: str,
    ) -> BackfillMarker | None:
        return self.markers.get((provider, account_id, environment, source, source_version))

    def save_marker(self, marker: BackfillMarker) -> None:
        self.markers[
            (
                marker.provider,
                marker.account_id,
                marker.environment,
                marker.source,
                marker.source_version,
            )
        ] = marker

    def write_payment_fact(self, event: TrustEvent) -> bool:
        if any(
            row.provider == event.provider
            and row.kind == "payment"
            and row.original_payment_ref == event.original_payment_ref
            for row in self.events.values()
        ):
            return False
        self.events[(event.workspace_id, event.event_id)] = dataclasses.replace(event)
        return True

    def write_adverse_fact(self, event: AdverseTrustEvent) -> str:
        payment = next(
            (
                row
                for row in self.events.values()
                if row.provider == event.provider
                and row.kind == "payment"
                and row.original_payment_ref == event.original_payment_ref
            ),
            None,
        )
        if payment is None:
            return "inbox"
        existing_key = next(
            (
                key
                for key, row in self.events.items()
                if row.provider == event.provider and row.adverse_ref == event.adverse_ref
            ),
            None,
        )
        if existing_key is not None:
            existing = self.events[existing_key]
            if existing.lifecycle_status == event.lifecycle_status:
                return "replay"
            if str(existing.provider_ordering_watermark) >= event.provider_ordering_watermark:
                return "stale"
            existing.amount_micro = event.amount_micro
            existing.lifecycle_status = event.lifecycle_status
            existing.provider_ordering_watermark = event.provider_ordering_watermark
            existing.provider_subtype = event.provider_subtype
            return "applied"
        self.events[(payment.workspace_id, event.event_id)] = TrustEvent(
            workspace_id=payment.workspace_id,
            event_id=event.event_id,
            kind=event.kind,
            provider=event.provider,
            amount_micro=event.amount_micro,
            original_payment_ref=event.original_payment_ref,
            adverse_ref=event.adverse_ref,
            occurred_at=event.occurred_at,
            recorded_at=NOW,
            payment_amount_micro=payment.payment_amount_micro,
            currency=payment.currency,
            credited_micro=payment.credited_micro,
            recovered_micro=None,
            provider_subtype=event.provider_subtype,
            lifecycle_status=event.lifecycle_status,
            cumulative_refunded=None,
            recovery_target=None,
            debit_status=None,
            unrecovered_micro=None,
            provider_ordering_watermark=event.provider_ordering_watermark,
        )
        return "applied"

    def list_provider_events(self, provider: str) -> tuple[TrustEvent, ...]:
        return tuple(row for row in self.events.values() if row.provider == provider)

    def list_outstanding(self, provider: str) -> tuple[OutstandingAdverse, ...]:
        return tuple(
            OutstandingAdverse(
                provider=row.provider,
                kind=row.kind,
                adverse_ref=str(row.adverse_ref),
                original_payment_ref=str(row.original_payment_ref),
                lifecycle_status=str(row.lifecycle_status),
                occurred_at=row.occurred_at,
            )
            for row in self.events.values()
            if row.provider == provider
            and (
                (row.kind == "refund" and row.lifecycle_status == "pending")
                or (
                    row.kind == "dispute"
                    and row.lifecycle_status not in {"won", "lost", "closed", "terminal_by_horizon"}
                )
            )
        )

    def replicate_workspace_watermark(
        self,
        workspace_id: str,
        qualifying_providers: frozenset[str],
        *,
        environment: str = "production",
    ) -> datetime | None:
        raise AssertionError("not used")


def _scan(*, refund_status: str = "succeeded") -> StripeTrustScan:
    return scan_stripe_responses(
        payment_intents=(_payment_intent(),),
        refunds=(_refund(status=refund_status),),
        disputes=(),
        recorded_at=NOW,
    )


def test_recorded_history_backfill_is_rerunnable_and_uses_pro_rata_target() -> None:
    repository = _Repository()
    scan = _scan()
    kwargs: dict[str, Any] = {
        "provider": "stripe",
        "account_id": "acct_1",
        "environment": "production",
        "source": "stripe-created-lists",
        "source_version": "stripe-trust-v1",
        "history_start": HISTORY_START,
        "closed_through": NOW - timedelta(minutes=15),
        "consistency_delay_seconds": 900,
        "now": NOW,
    }
    first = run_historical_backfill(repository, scan, **kwargs)
    second = run_historical_backfill(repository, scan, **kwargs)

    assert first.marker.is_complete
    assert first.marker.closed_through == kwargs["closed_through"]
    assert first.watermark_advanced
    assert second.marker.is_complete
    assert len(repository.events) == 2
    canonical = {
        row.key: row for row in canonical_records_from_events(repository.events.values())
    }
    # 60 refunded cents out of a fee-inclusive 120-cent payment recovers half
    # of the 1,000,000 credited principal. Raw-cents subtraction gives 400,000.
    assert canonical[("stripe", "payment", "pi_fee")].recovery_target == 500_000


def test_unmatched_id_keeps_initial_safe_watermark() -> None:
    repository = _Repository()
    scan = scan_stripe_responses(
        payment_intents=(),
        refunds=(_refund(),),
        disputes=(),
        recorded_at=NOW,
    )

    result = run_historical_backfill(
        repository,
        scan,
        provider="stripe",
        account_id="acct_1",
        environment="production",
        source="stripe-created-lists",
        source_version="stripe-trust-v1",
        history_start=HISTORY_START,
        closed_through=NOW - timedelta(minutes=15),
        consistency_delay_seconds=900,
        now=NOW,
    )

    assert result.marker.unmatched_count == 1
    assert result.marker.closed_through == HISTORY_START
    assert not result.watermark_advanced


def test_canonical_hash_mismatch_keeps_initial_safe_watermark() -> None:
    repository = _Repository()
    scan = _scan()
    assert repository.write_payment_fact(
        dataclasses.replace(scan.payments[0], workspace_id="wrong_workspace")
    )

    result = run_historical_backfill(
        repository,
        scan,
        provider="stripe",
        account_id="acct_1",
        environment="production",
        source="stripe-created-lists",
        source_version="stripe-trust-v1",
        history_start=HISTORY_START,
        closed_through=NOW - timedelta(minutes=15),
        consistency_delay_seconds=900,
        now=NOW,
    )

    assert result.marker.semantic_mismatch_count > 0
    assert result.marker.closed_through == HISTORY_START
    assert not result.watermark_advanced


def test_recorded_x402_payment_and_refund_use_the_same_canonical_pipeline() -> None:
    payment = _payment_intent(payment_intent_id="pi_x402", x402=True)
    payment["amount_received"] = 100
    scan = scan_stripe_responses(
        payment_intents=(payment,),
        refunds=(_refund(payment_intent_id="pi_x402"),),
        disputes=(),
        recorded_at=NOW,
    )
    assert {row.provider for row in scan.payments} == {"x402"}
    assert {row.provider for row in scan.adverse} == {"x402"}
    canonical = canonical_mapping(canonical_records_from_events(scan.source_events))
    assert set(canonical) == {
        ("x402", "payment", "pi_x402"),
        ("x402", "refund", "re_fee"),
    }
    records = {
        row.key: row for row in canonical_records_from_events(scan.source_events)
    }
    payment_record = records[("x402", "payment", "pi_x402")]
    assert payment_record.payment_amount_micro == 1_000_000
    assert payment_record.recovery_target == 600_000


@pytest.mark.parametrize("requirement", [
    MarkerRequirement("stripe", "acct_1", "production", "stripe-created-lists", "v1"),
    MarkerRequirement("owner_inventory", "local", "production", "tr_entities.workspace", "rev-1"),
])
def test_completion_rule_and_exact_arm_marker_predicate(requirement: MarkerRequirement) -> None:
    complete = BackfillMarker(
        provider=requirement.provider,
        account_id=requirement.account_id,
        environment=requirement.environment,
        source=requirement.source,
        source_version=requirement.source_version,
        history_start=HISTORY_START,
        closed_through=NOW - timedelta(minutes=15),
        consistency_delay_seconds=900,
        unmatched_count=0,
        semantic_mismatch_count=0,
        completed_at=NOW,
    )
    assert completed_marker_satisfies(complete, requirement)
    assert not completed_marker_satisfies(None, requirement)
    for column in ("provider", "account_id", "environment", "source", "source_version"):
        assert not completed_marker_satisfies(
            dataclasses.replace(complete, **{column: "other"}), requirement
        )
    assert not completed_marker_satisfies(
        dataclasses.replace(complete, source_version="v2"), requirement
    )
    assert not completed_marker_satisfies(
        dataclasses.replace(complete, completed_at=None), requirement
    )
    with pytest.raises(ValueError, match="completed_at requires"):
        dataclasses.replace(complete, unmatched_count=1)


def test_two_way_mapping_reports_missing_both_directions_and_injected_mismatch() -> None:
    base = CanonicalTrustRecord(
        provider="stripe",
        kind="payment",
        provider_subtype="checkout",
        adverse_ref=None,
        original_payment_ref="pi_1",
        lifecycle_status="succeeded",
        payment_amount_micro=1_000_000,
        currency="USD",
        credited_micro=1_000_000,
        recovery_target=0,
        workspace_id="ws_1",
        occurred_at=NOW,
        provider_ordering_watermark="1:pi_1",
    )
    source = canonical_mapping(
        [base, dataclasses.replace(base, original_payment_ref="pi_source")]
    )
    local = canonical_mapping(
        [
            dataclasses.replace(base, workspace_id="wrong_workspace"),
            dataclasses.replace(base, original_payment_ref="pi_local"),
        ]
    )
    diff = reconcile_canonical_mappings(source, local)
    assert diff.source_only == (("stripe", "payment", "pi_source"),)
    assert diff.local_only == (("stripe", "payment", "pi_local"),)
    assert diff.semantic_mismatches == (("stripe", "payment", "pi_1"),)


def test_outstanding_horizons_and_freshness_boundaries() -> None:
    refund = OutstandingAdverse(
        "stripe", "refund", "re_1", "pi_1", "pending", NOW - timedelta(days=30)
    )
    dispute = OutstandingAdverse(
        "stripe",
        "dispute",
        "dp_1",
        "pi_1",
        "needs_response",
        NOW - timedelta(days=100),
        NOW - timedelta(days=90),
    )
    assert outstanding_is_beyond_horizon(refund, now=NOW)
    assert outstanding_is_beyond_horizon(dispute, now=NOW)
    assert reconciliation_tail_start(NOW, consistency_delay_seconds=900, cadence_seconds=900) == (
        NOW - timedelta(minutes=45)
    )
    assert trust_reconciliation_is_fresh(
        NOW - timedelta(hours=1), now=NOW, max_age_seconds=3600
    )
    assert not trust_reconciliation_is_fresh(
        NOW - timedelta(hours=1, seconds=1), now=NOW, max_age_seconds=3600
    )
    assert not trust_reconciliation_is_fresh(None, now=NOW, max_age_seconds=3600)


def test_nonterminal_dispute_and_refund_are_refetched_until_terminal() -> None:
    repository = _Repository()
    scan = scan_stripe_responses(
        payment_intents=(_payment_intent(),),
        refunds=(_refund(status="pending"),),
        disputes=(_dispute(),),
        recorded_at=NOW,
    )
    # The reconciliation model accepts provider lifecycle states directly. Preserve
    # this recorded provider state instead of the scanner's recovery-oriented alias.
    scan = dataclasses.replace(
        scan,
        adverse=tuple(
            dataclasses.replace(row, lifecycle_status="needs_response")
            if row.kind == "dispute"
            else row
            for row in scan.adverse
        ),
        source_events=tuple(
            dataclasses.replace(row, lifecycle_status="needs_response")
            if row.kind == "dispute"
            else row
            for row in scan.source_events
        ),
    )
    run_historical_backfill(
        repository,
        scan,
        provider="stripe",
        account_id="acct_1",
        environment="production",
        source="stripe-created-lists",
        source_version="stripe-trust-v1",
        history_start=HISTORY_START,
        closed_through=NOW - timedelta(minutes=15),
        consistency_delay_seconds=900,
        now=NOW,
    )
    assert {row.adverse_ref for row in repository.list_outstanding("stripe")} == {
        "dp_fee",
        "re_fee",
    }

    events = {str(row.adverse_ref): row for row in scan.adverse}
    refetched: list[str] = []

    def refetch_nonterminal(
        row: OutstandingAdverse, _now: datetime
    ) -> tuple[AdverseTrustEvent, OutstandingAdverse]:
        refetched.append(row.adverse_ref)
        return dataclasses.replace(
            events[row.adverse_ref],
            lifecycle_status=("needs_response" if row.kind == "dispute" else "pending"),
            provider_ordering_watermark="00000000000000000003:still_open",
        ), dataclasses.replace(
            row,
            lifecycle_status=("needs_response" if row.kind == "dispute" else "pending"),
            occurred_at=NOW,
            evidence_deadline=NOW,
        )

    first = run_recurring_reconciliation(
        repository,
        lambda _start, _end: StripeTrustScan((), (), (), (), ()),
        refetch_nonterminal,
        provider="stripe",
        account_id="acct_1",
        environment="production",
        source="stripe-created-lists",
        source_version="stripe-trust-v1",
        cadence_seconds=900,
        now=NOW + timedelta(minutes=15),
        alert_horizon=lambda _row: None,
    )
    assert first.outstanding_count == 2
    assert set(refetched) == {"dp_fee", "re_fee"}
    assert {row.adverse_ref for row in repository.list_outstanding("stripe")} == {
        "dp_fee",
        "re_fee",
    }

    def refetch_terminal(
        row: OutstandingAdverse, _now: datetime
    ) -> tuple[AdverseTrustEvent, OutstandingAdverse]:
        status = "won" if row.kind == "dispute" else "succeeded"
        return dataclasses.replace(
            events[row.adverse_ref],
            lifecycle_status=status,
            provider_ordering_watermark="00000000000000000004:terminal",
        ), dataclasses.replace(row, lifecycle_status=status)

    second = run_recurring_reconciliation(
        repository,
        lambda _start, _end: StripeTrustScan((), (), (), (), ()),
        refetch_terminal,
        provider="stripe",
        account_id="acct_1",
        environment="production",
        source="stripe-created-lists",
        source_version="stripe-trust-v1",
        cadence_seconds=900,
        now=NOW + timedelta(minutes=30),
        alert_horizon=lambda _row: None,
    )
    assert second.outstanding_count == 2
    assert repository.list_outstanding("stripe") == ()


def test_dispute_horizon_terminal_preserves_an_already_active_full_claim() -> None:
    payment = _scan().payments[0]
    adverse = TrustEvent(
        workspace_id=payment.workspace_id,
        event_id="dispute",
        kind="dispute",
        provider="stripe",
        amount_micro=payment.payment_amount_micro,
        original_payment_ref=payment.original_payment_ref,
        adverse_ref="dp_1",
        occurred_at=NOW - timedelta(days=100),
        recorded_at=NOW,
        payment_amount_micro=payment.payment_amount_micro,
        currency=payment.currency,
        credited_micro=payment.credited_micro,
        recovered_micro=None,
        provider_subtype="dispute",
        lifecycle_status="terminal_by_horizon",
        cumulative_refunded=0,
        recovery_target=payment.credited_micro,
        debit_status="debited",
        unrecovered_micro=0,
        provider_ordering_watermark="2:terminal",
    )
    records = canonical_records_from_events((payment, adverse))
    assert {row.recovery_target for row in records} == {payment.credited_micro}


def test_failed_outstanding_refetch_leaves_watermark_unchanged() -> None:
    repository = _Repository()
    pending = _scan(refund_status="pending")
    run_historical_backfill(
        repository,
        pending,
        provider="stripe",
        account_id="acct_1",
        environment="production",
        source="stripe-created-lists",
        source_version="stripe-trust-v1",
        history_start=HISTORY_START,
        closed_through=NOW - timedelta(minutes=15),
        consistency_delay_seconds=900,
        now=NOW,
    )
    before = repository.get_marker(
        "stripe", "acct_1", "production", "stripe-created-lists", "stripe-trust-v1"
    )
    assert before is not None

    def failed(_row: OutstandingAdverse, _now: datetime) -> Any:
        raise RuntimeError("recorded provider failure")

    result = run_recurring_reconciliation(
        repository,
        lambda _start, _end: StripeTrustScan((), (), (), (), ()),
        failed,
        provider="stripe",
        account_id="acct_1",
        environment="production",
        source="stripe-created-lists",
        source_version="stripe-trust-v1",
        cadence_seconds=900,
        now=NOW + timedelta(minutes=15),
        alert_horizon=lambda _row: None,
    )
    assert result.marker.closed_through == before.closed_through
    assert result.marker.completed_at is None
    assert result.marker.unmatched_count == 1
    assert not result.watermark_advanced


def test_horizon_marks_terminal_through_adverse_writer_and_alerts() -> None:
    repository = _Repository()
    old_created = NOW - timedelta(days=31)
    scan = scan_stripe_responses(
        payment_intents=(_payment_intent(created=old_created - timedelta(days=1)),),
        refunds=(_refund(status="pending", created=old_created, watermark="1:pending"),),
        disputes=(),
        recorded_at=NOW,
    )
    result = run_historical_backfill(
        repository,
        scan,
        provider="stripe",
        account_id="acct_1",
        environment="production",
        source="stripe-created-lists",
        source_version="stripe-trust-v1",
        history_start=HISTORY_START,
        closed_through=NOW - timedelta(minutes=15),
        consistency_delay_seconds=900,
        now=NOW,
    )
    assert result.marker.is_complete
    alerts: list[str] = []

    def refetch(row: OutstandingAdverse, _now: datetime) -> tuple[AdverseTrustEvent, OutstandingAdverse]:
        event = next(item for item in scan.adverse if item.adverse_ref == row.adverse_ref)
        return dataclasses.replace(event, provider_ordering_watermark="2:pending"), dataclasses.replace(
            row, occurred_at=old_created
        )

    recurring = run_recurring_reconciliation(
        repository,
        lambda _start, _end: StripeTrustScan((), (), (), (), ()),
        refetch,
        provider="stripe",
        account_id="acct_1",
        environment="production",
        source="stripe-created-lists",
        source_version="stripe-trust-v1",
        cadence_seconds=900,
        now=NOW + timedelta(minutes=15),
        alert_horizon=lambda row: alerts.append(row.adverse_ref),
    )
    adverse = next(row for row in repository.events.values() if row.kind == "refund")
    assert adverse.lifecycle_status == "terminal_by_horizon"
    assert alerts == ["re_fee"]
    assert recurring.watermark_advanced


def test_spanner_shard_replication_uses_minimum_completed_provider_watermark() -> None:
    stripe_time = NOW - timedelta(minutes=20)
    x402_time = NOW - timedelta(minutes=25)

    class Tx:
        updated_params: dict[str, Any] = {}

        def execute_sql(self, sql: str, **kwargs: Any) -> list[tuple[Any, ...]]:
            if sql.startswith("SELECT DISTINCT provider"):
                return [("stripe",), ("x402",)]
            if sql.startswith("SELECT closed_through"):
                provider = kwargs["params"]["provider"]
                return [(stripe_time if provider == "stripe" else x402_time,)]
            if sql.startswith("SELECT shard"):
                return [(0,), (1,), (2,)]
            raise AssertionError(sql)

        def execute_update(self, sql: str, **kwargs: Any) -> int:
            assert "trust_reconciled_through=@watermark" in sql
            self.updated_params = kwargs["params"]
            return 3

    tx = Tx()
    store = SimpleNamespace(
        _param_types=SimpleNamespace(STRING="STRING", TIMESTAMP="TIMESTAMP"),
        _run_in_transaction=lambda callback: callback(tx),
    )
    repository = SpannerTrustReconciliationRepository(store)
    result = repository.replicate_workspace_watermark(
        "ws_1", frozenset({"stripe", "x402"})
    )
    assert result == x402_time
    assert tx.updated_params == {"watermark": x402_time, "workspace_id": "ws_1"}


def _create_columns(ddl: str, table: str) -> tuple[str, ...]:
    match = re.search(
        rf"CREATE TABLE(?: IF NOT EXISTS)? {table} \((.*?)\n\s*(?:\) PRIMARY KEY|\);)",
        ddl,
        flags=re.DOTALL,
    )
    assert match is not None
    return tuple(
        line.strip().split()[0]
        for line in match.group(1).splitlines()
        if line.strip()
        and len(line.strip().split()) > 1
        and line.strip().split()[1].startswith(
            ("STRING", "TIMESTAMP", "INT64", "TEXT", "TIMESTAMPTZ", "BIGINT")
        )
    )


@pytest.mark.parametrize("migration", [
    "migrate_trust_reconciliation.sh", "migrate_typed_counters.sh",
])
def test_marker_ddl_exact_columns_completion_check_and_explicit_conflict_target(
    migration: str,
) -> None:
    spanner = (ROOT / "scripts/deploy" / migration).read_text()
    postgres = (ROOT / "src/trusted_router/storage_postgres_schema.sql").read_text()
    storage = (ROOT / "src/trusted_router/storage_trust_reconciliation.py").read_text()
    assert _create_columns(spanner, "tr_trust_backfill") == MARKER_COLUMNS
    assert _create_columns(postgres, "tr_trust_backfill") == MARKER_COLUMNS
    completion = "completed_at IS NULL OR (unmatched_count = 0 AND semantic_mismatch_count = 0)"
    assert completion in " ".join(spanner.split())
    assert completion in " ".join(postgres.split())
    for ddl in (spanner, postgres):
        assert len(re.findall(r"CREATE TABLE(?: IF NOT EXISTS)? tr_trust_backfill \(", ddl)) == 1
        assert "PRIMARY KEY (provider, account_id, environment, source, source_version)" in ddl
        for column in ("consistency_delay_seconds", "unmatched_count", "semantic_mismatch_count"):
            assert f"{column} >= 0" in ddl
    target = "ON CONFLICT (provider, account_id, environment, source, source_version)"
    assert target in storage
    targetless = re.compile(r"\bON\s+CONFLICT\s+DO\s+NOTHING\b", re.IGNORECASE)
    assert not any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and targetless.search(node.value)
        for node in ast.walk(ast.parse(storage))
    )


def test_cloud_run_job_is_pinned_inert_and_scheduled_from_interval() -> None:
    deploy = (ROOT / "scripts/deploy/trust_reconciler.sh").read_text()
    backfill = (ROOT / "scripts/deploy/trust_backfill_job.sh").read_text()
    assert '"TR_TRUST_RECONCILE_INTERVAL_SECONDS=900"' in deploy
    assert '"TR_TRUST_RECONCILE_MAX_AGE_SECONDS=3600"' in deploy
    assert '"TR_SPEND_LEASE_TRUST_ELIGIBILITY_ENABLED=false"' in deploy
    assert '--schedule="*/15 * * * *"' in deploy
    assert '"TR_SPEND_LEASE_TRUST_ELIGIBILITY_ENABLED=false"' in backfill
    assert "--drain-window-start" in backfill
    assert "jobs execute" not in backfill
