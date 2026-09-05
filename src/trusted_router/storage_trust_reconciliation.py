"""Backend adapters for durable trust backfill markers and shard watermarks."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Protocol

from trusted_router.storage_gcp_trust import (
    TRUST_EVENT_COLUMNS,
    drain_matching_trust_inbox_tx,
    insert_credit_trust_event,
)
from trusted_router.storage_models import AdverseTrustEvent, CreditProvenance, TrustEvent
from trusted_router.trust_reconciliation import (
    STRIPE_TRUST_SOURCE,
    STRIPE_TRUST_SOURCE_VERSION,
    BackfillMarker,
    OutstandingAdverse,
)

MARKER_KEY_COLUMNS = (
    "provider",
    "account_id",
    "environment",
    "source",
    "source_version",
)
MARKER_COLUMNS = (
    *MARKER_KEY_COLUMNS,
    "history_start",
    "closed_through",
    "consistency_delay_seconds",
    "unmatched_count",
    "semantic_mismatch_count",
    "completed_at",
)


class TrustReconciliationRepository(Protocol):
    def get_marker(
        self,
        provider: str,
        account_id: str,
        environment: str,
        source: str,
        source_version: str,
    ) -> BackfillMarker | None: ...

    def save_marker(self, marker: BackfillMarker) -> None: ...
    def write_payment_fact(self, event: TrustEvent) -> bool: ...
    def write_adverse_fact(self, event: AdverseTrustEvent) -> str: ...
    def list_provider_events(self, provider: str) -> tuple[TrustEvent, ...]: ...
    def list_outstanding(self, provider: str) -> tuple[OutstandingAdverse, ...]: ...
    def replicate_workspace_watermark(
        self,
        workspace_id: str,
        qualifying_providers: frozenset[str],
        *,
        environment: str = "production",
    ) -> datetime | None: ...


def _event_from_row(row: Any) -> TrustEvent:
    values = list(row)
    for index in (7, 8):
        if isinstance(values[index], str):
            values[index] = datetime.fromisoformat(values[index].replace("Z", "+00:00"))
    return TrustEvent(*values)


def _marker_from_row(row: Any) -> BackfillMarker:
    values = list(row)
    for index in (5, 6, 10):
        if isinstance(values[index], str):
            values[index] = datetime.fromisoformat(values[index].replace("Z", "+00:00"))
    return BackfillMarker(*values)


def _outstanding(events: Iterable[TrustEvent]) -> tuple[OutstandingAdverse, ...]:
    rows: list[OutstandingAdverse] = []
    for event in events:
        if event.kind == "refund" and event.lifecycle_status == "pending":
            pass
        elif event.kind == "dispute" and event.lifecycle_status not in {
            "won",
            "lost",
            "closed",
            "terminal_by_horizon",
        }:
            pass
        else:
            continue
        assert event.adverse_ref is not None
        assert event.original_payment_ref is not None
        rows.append(
            OutstandingAdverse(
                provider=event.provider,
                kind=event.kind,
                adverse_ref=event.adverse_ref,
                original_payment_ref=event.original_payment_ref,
                lifecycle_status=str(event.lifecycle_status),
                occurred_at=event.occurred_at,
            )
        )
    return tuple(sorted(rows, key=lambda row: (row.occurred_at, row.adverse_ref)))


class SpannerTrustReconciliationRepository:
    def __init__(self, store: Any) -> None:
        self.store = store

    def get_marker(
        self,
        provider: str,
        account_id: str,
        environment: str,
        source: str,
        source_version: str,
    ) -> BackfillMarker | None:
        query = (
            "SELECT " + ", ".join(MARKER_COLUMNS) + " FROM tr_trust_backfill "  # noqa: S608 - fixed columns.
            "WHERE provider=@provider AND account_id=@account_id "
            "AND environment=@environment AND source=@source "
            "AND source_version=@source_version"
        )
        params = dict(
            zip(
                MARKER_KEY_COLUMNS,
                (provider, account_id, environment, source, source_version),
                strict=True,
            )
        )
        with self.store._database.snapshot() as snapshot:
            rows = list(
                snapshot.execute_sql(
                    query,
                    params=params,
                    param_types={key: self.store._param_types.STRING for key in params},
                )
            )
        if len(rows) > 1:
            raise RuntimeError("trust backfill marker primary key is not unique")
        return None if not rows else _marker_from_row(rows[0])

    def save_marker(self, marker: BackfillMarker) -> None:
        completed_at = marker.completed_at if marker.is_complete else None

        def txn(transaction: Any) -> None:
            params = dataclasses.asdict(marker)
            params["completed_at"] = completed_at
            types = self.store._param_types
            updated = transaction.execute_update(
                "UPDATE tr_trust_backfill SET source=@source, source_version=@source_version, "
                "history_start=@history_start, "
                "closed_through=@closed_through, "
                "consistency_delay_seconds=@consistency_delay_seconds, "
                "unmatched_count=@unmatched_count, "
                "semantic_mismatch_count=@semantic_mismatch_count, "
                "completed_at=@completed_at WHERE provider=@provider "
                "AND account_id=@account_id AND environment=@environment",
                params=params,
                param_types={
                    **{key: types.STRING for key in MARKER_KEY_COLUMNS},
                    "history_start": types.TIMESTAMP,
                    "closed_through": types.TIMESTAMP,
                    "consistency_delay_seconds": types.INT64,
                    "unmatched_count": types.INT64,
                    "semantic_mismatch_count": types.INT64,
                    "completed_at": types.TIMESTAMP,
                },
            )
            if int(updated) == 0:
                transaction.insert(
                    table="tr_trust_backfill",
                    columns=MARKER_COLUMNS,
                    values=[tuple(params[column] for column in MARKER_COLUMNS)],
                )
            elif int(updated) != 1:
                raise RuntimeError("trust backfill marker update was not unique")

        self.store._run_in_transaction(txn)

    def write_payment_fact(self, event: TrustEvent) -> bool:
        def txn(transaction: Any) -> tuple[bool, tuple[Any, ...]]:
            inserted = insert_credit_trust_event(
                transaction, self.store._param_types, event
            )
            drained: tuple[Any, ...] = ()
            if event.original_payment_ref is not None:
                drained = drain_matching_trust_inbox_tx(
                    transaction,
                    self.store._param_types,
                    provider=event.provider,
                    original_payment_ref=event.original_payment_ref,
                    now=event.recorded_at,
                    read_entity_tx=self.store._read_entity_tx,
                    write_entity_tx=self.store._write_entity_trust_dml_tx,
                )
            return inserted, drained

        inserted, drained = self.store._run_in_transaction(txn)
        for result in drained:
            self.store._alert_unrecovered_principal(result)
        return bool(inserted)

    def write_adverse_fact(self, event: AdverseTrustEvent) -> str:
        return str(self.store.record_adverse_trust_event(event).outcome)

    def list_provider_events(self, provider: str) -> tuple[TrustEvent, ...]:
        query = (
            "SELECT " + ", ".join(TRUST_EVENT_COLUMNS) + " FROM tr_trust_event "  # noqa: S608 - fixed columns.
            "WHERE provider=@provider ORDER BY occurred_at, event_id"
        )
        with self.store._database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                query,
                params={"provider": provider},
                param_types={"provider": self.store._param_types.STRING},
            )
            return tuple(_event_from_row(row) for row in rows)

    def list_outstanding(self, provider: str) -> tuple[OutstandingAdverse, ...]:
        return _outstanding(self.list_provider_events(provider))

    def replicate_workspace_watermark(
        self,
        workspace_id: str,
        qualifying_providers: frozenset[str],
        *,
        environment: str = "production",
    ) -> datetime | None:
        types = self.store._param_types

        def txn(transaction: Any) -> datetime | None:
            provider_rows = list(
                transaction.execute_sql(
                    "SELECT DISTINCT provider FROM tr_trust_event "
                    "WHERE workspace_id=@workspace_id AND kind='payment'",
                    params={"workspace_id": workspace_id},
                    param_types={"workspace_id": types.STRING},
                )
            )
            providers = {str(row[0]) for row in provider_rows} & qualifying_providers
            watermarks: list[datetime] = []
            for provider in sorted(providers):
                rows = list(
                    transaction.execute_sql(
                        "SELECT closed_through FROM tr_trust_backfill "
                        "WHERE provider=@provider AND completed_at IS NOT NULL "
                        "AND unmatched_count=0 AND semantic_mismatch_count=0 "
                        "AND environment=@environment AND source=@source "
                        "AND source_version=@source_version",
                        params={
                            "provider": provider,
                            "environment": environment,
                            "source": STRIPE_TRUST_SOURCE,
                            "source_version": STRIPE_TRUST_SOURCE_VERSION,
                        },
                        param_types={
                            "provider": types.STRING,
                            "environment": types.STRING,
                            "source": types.STRING,
                            "source_version": types.STRING,
                        },
                    )
                )
                if len(rows) != 1:
                    watermarks = []
                    break
                watermarks.append(rows[0][0])
            reconciled = min(watermarks) if watermarks and providers else None
            shard_rows = list(
                transaction.execute_sql(
                    "SELECT shard FROM tr_credit_balance "
                    "WHERE workspace_id=@workspace_id ORDER BY shard",
                    params={"workspace_id": workspace_id},
                    param_types={"workspace_id": types.STRING},
                )
            )
            updated = transaction.execute_update(
                "UPDATE tr_credit_balance SET trust_reconciled_through=@watermark "
                "WHERE workspace_id=@workspace_id",
                params={"watermark": reconciled, "workspace_id": workspace_id},
                param_types={"watermark": types.TIMESTAMP, "workspace_id": types.STRING},
            )
            if int(updated) != len(shard_rows):
                raise RuntimeError("trust watermark replication missed an active shard")
            return reconciled

        return self.store._run_in_transaction(txn)


class PostgresTrustReconciliationRepository:
    def __init__(self, store: Any) -> None:
        self.store = store

    def get_marker(
        self,
        provider: str,
        account_id: str,
        environment: str,
        source: str,
        source_version: str,
    ) -> BackfillMarker | None:
        def read(conn: Any) -> BackfillMarker | None:
            row = conn.execute(
                "SELECT " + ", ".join(MARKER_COLUMNS) + " FROM tr_trust_backfill "  # noqa: S608 - fixed columns.
                "WHERE provider=%s AND account_id=%s AND environment=%s "
                "AND source=%s AND source_version=%s",
                (provider, account_id, environment, source, source_version),
            ).fetchone()
            return None if row is None else _marker_from_row(row)

        return self.store._run_transaction(read)

    def save_marker(self, marker: BackfillMarker) -> None:
        completed_at = marker.completed_at if marker.is_complete else None
        values = [getattr(marker, column) for column in MARKER_COLUMNS]
        values[-1] = completed_at

        def write(conn: Any) -> None:
            conn.execute(
                "INSERT INTO tr_trust_backfill (" + ", ".join(MARKER_COLUMNS) + ") "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (provider, account_id, environment) "
                "DO UPDATE SET source=EXCLUDED.source, source_version=EXCLUDED.source_version, "
                "history_start=EXCLUDED.history_start, "
                "closed_through=EXCLUDED.closed_through, "
                "consistency_delay_seconds=EXCLUDED.consistency_delay_seconds, "
                "unmatched_count=EXCLUDED.unmatched_count, "
                "semantic_mismatch_count=EXCLUDED.semantic_mismatch_count, "
                "completed_at=EXCLUDED.completed_at",
                tuple(values),
            )

        self.store._run_transaction(write)

    def write_payment_fact(self, event: TrustEvent) -> bool:
        provenance = CreditProvenance(
            source=str(event.provider_subtype),
            provider=event.provider,
            external_ref=event.original_payment_ref,
            occurred_at=event.occurred_at,
        )

        def write(conn: Any) -> bool:
            inserted = self.store._insert_credit_trust_event_tx(
                conn,
                workspace_id=event.workspace_id,
                event_id=event.event_id,
                amount_microdollars=int(event.credited_micro or 0),
                provenance=provenance,
                recorded_at=event.recorded_at,
                payment_amount_microdollars=event.payment_amount_micro,
                currency=event.currency,
            )
            if inserted:
                conn.execute(
                    "UPDATE tr_trust_event SET provider_ordering_watermark=%s "
                    "WHERE workspace_id=%s AND event_id=%s AND kind='payment'",
                    (
                        event.provider_ordering_watermark,
                        event.workspace_id,
                        event.event_id,
                    ),
                )
            return bool(inserted)

        return bool(self.store._run_transaction(write))

    def write_adverse_fact(self, event: AdverseTrustEvent) -> str:
        return str(self.store.record_adverse_trust_event(event).outcome)

    def list_provider_events(self, provider: str) -> tuple[TrustEvent, ...]:
        def read(conn: Any) -> tuple[TrustEvent, ...]:
            rows = conn.execute(
                "SELECT " + ", ".join(TRUST_EVENT_COLUMNS) + " FROM tr_trust_event "  # noqa: S608 - fixed columns.
                "WHERE provider=%s ORDER BY occurred_at, event_id",
                (provider,),
            ).fetchall()
            return tuple(_event_from_row(row) for row in rows)

        return self.store._run_transaction(read)

    def list_outstanding(self, provider: str) -> tuple[OutstandingAdverse, ...]:
        return _outstanding(self.list_provider_events(provider))

    def replicate_workspace_watermark(
        self,
        workspace_id: str,
        qualifying_providers: frozenset[str],
        *,
        environment: str = "production",
    ) -> datetime | None:
        def write(conn: Any) -> datetime | None:
            provider_rows = conn.execute(
                "SELECT DISTINCT provider FROM tr_trust_event "
                "WHERE workspace_id=%s AND kind='payment'",
                (workspace_id,),
            ).fetchall()
            providers = {str(row[0]) for row in provider_rows} & qualifying_providers
            watermarks: list[datetime] = []
            for provider in sorted(providers):
                rows = conn.execute(
                    "SELECT closed_through FROM tr_trust_backfill "
                    "WHERE provider=%s AND completed_at IS NOT NULL "
                    "AND unmatched_count=0 AND semantic_mismatch_count=0 "
                    "AND environment=%s AND source=%s AND source_version=%s",
                    (
                        provider,
                        environment,
                        STRIPE_TRUST_SOURCE,
                        STRIPE_TRUST_SOURCE_VERSION,
                    ),
                ).fetchall()
                if len(rows) != 1:
                    watermarks = []
                    break
                watermarks.append(rows[0][0])
            reconciled = min(watermarks) if watermarks and providers else None
            shard_rows = conn.execute(
                "SELECT shard FROM tr_credit_balance WHERE workspace_id=%s ORDER BY shard",
                (workspace_id,),
            ).fetchall()
            updated = conn.execute(
                "UPDATE tr_credit_balance SET trust_reconciled_through=%s "
                "WHERE workspace_id=%s",
                (reconciled, workspace_id),
            )
            if updated.rowcount != len(shard_rows):
                raise RuntimeError("trust watermark replication missed an active shard")
            return reconciled

        return self.store._run_transaction(write)


def trust_reconciliation_repository(store: Any) -> TrustReconciliationRepository:
    target = getattr(store, "_backend", store)
    if hasattr(target, "_database") and hasattr(target, "_param_types"):
        return SpannerTrustReconciliationRepository(target)
    if hasattr(target, "_run_transaction"):
        return PostgresTrustReconciliationRepository(target)
    raise TypeError(f"unsupported trust reconciliation store: {type(target).__name__}")


def replicate_tier_job_watermark(
    store: Any,
    workspace_id: str,
    qualifying_providers: frozenset[str],
    *,
    environment: str,
) -> tuple[bool, datetime | None]:
    """Replicate when supported, while preserving lightweight tier-job fakes."""

    replicate = getattr(store, "replicate_workspace_trust_reconciled_through", None)
    if not callable(replicate):
        target = getattr(store, "_backend", store)
        database = getattr(target, "_database", None)
        fake_tables = getattr(database, "typed", None)
        if isinstance(fake_tables, dict) and "tr_trust_backfill" not in fake_tables:
            return False, None
        try:
            replicate = trust_reconciliation_repository(store).replicate_workspace_watermark
        except TypeError:
            return False, None
    return True, replicate(
        workspace_id,
        qualifying_providers,
        environment=environment,
    )
