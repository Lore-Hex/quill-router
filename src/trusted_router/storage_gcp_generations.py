"""Generation records with durable ClickHouse delivery.

Sibling of InMemoryGenerations. The general ``add()`` path remains compatible
with non-gateway callers and rolling legacy records:
  1. add_usage_to_key — roll cost into per-key counters (own txn).
  2. Spanner txn — generation row + workspace index entry.
  3. Durable ClickHouse outbox enqueue.

The high-volume gateway path does not call ``add()``. Billing settles in typed
Spanner tables and atomically enqueues bounded ClickHouse metadata. Its durable
settle outbox retains repair inputs until delivery durability is confirmed.
Tenant-facing activity and usage reads are served from ClickHouse by the
store; this adapter owns only the Spanner side of a generation."""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, overload

from trusted_router.storage_gcp_analytics_outbox import SpannerAnalyticsOutbox
from trusted_router.storage_gcp_codec import (
    generation_workspace_id as _generation_workspace_id,
)
from trusted_router.storage_gcp_generation_records import (
    read_generation_record,
    upsert_generation_record,
)
from trusted_router.storage_gcp_io import SpannerIO
from trusted_router.storage_models import (
    Generation,
    ProviderBenchmarkSample,
    _is_byok,
)
from trusted_router.storage_operational_analytics import (
    OperationalAnalyticsWriter,
)

log = logging.getLogger(__name__)

ACTIVITY_DELIVERY_REPAIR = (
    "python -m trusted_router.activity_delivery_repair_cli --generation-id <generation_id>"
)


@dataclass
class ActivityReconcileResult:
    durable_repaired: int = 0
    durable_failed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    scanned: int = 0
    truncated: bool = False
    next_after_id: str | None = None


class _AddUsageCallback(Protocol):
    def __call__(self, key_hash: str, cost_microdollars: int, *, is_byok: bool) -> None: ...


class SpannerGenerations:
    def __init__(
        self,
        io: SpannerIO,
        *,
        param_types: Any | None = None,
        generation_records_enabled: bool = False,
        add_usage_to_key: _AddUsageCallback,
        analytics_outbox: SpannerAnalyticsOutbox | None = None,
        operational_analytics_outbox: OperationalAnalyticsWriter | None = None,
    ) -> None:
        self._io = io
        self._param_types = param_types
        self._generation_records_enabled = generation_records_enabled
        self._add_usage_to_key = add_usage_to_key
        self._analytics_outbox = analytics_outbox
        self._operational_analytics_outbox = operational_analytics_outbox

    def add(self, generation: Generation) -> None:
        # Two separate transactions instead of one fused one. Per-key
        # counters are not load-bearing for billing; the credit ledger is.
        self._add_usage_to_key(
            generation.key_hash,
            generation.total_cost_microdollars,
            is_byok=_is_byok(generation.usage_type),
        )

        def txn(transaction: Any) -> None:
            self._io.write_entity_tx(transaction, "generation", generation.id, generation)
            self._io.write_entity_tx(
                transaction,
                "generation_by_workspace",
                _generation_workspace_id(generation),
                {"generation_id": generation.id},
            )

        self._io.database.run_in_transaction(txn)
        self.index_after_commit(generation)

    def index_after_commit(self, generation: Generation) -> bool:
        """Repair durable delivery, then record the loss-tolerant benchmark.

        New typed settlements enqueue activity in their billing transaction and
        call ``post_commit_analytics`` instead. This method remains for an old
        settlement replay whose original commit may predate the atomic outbox.
        Without a durable outbox there is no activity delivery to repair, so the
        settlement has nothing pending once the benchmark is recorded.
        """
        if self._operational_analytics_outbox is None:
            self.post_commit_analytics(generation)
            return True
        activity_queued = self._repair_durable_delivery(generation)
        self.post_commit_analytics(generation)
        return activity_queued

    @property
    def analytics_outbox(self) -> SpannerAnalyticsOutbox | None:
        """The benchmark outbox, when configured (the one-commit settle batches it)."""
        return self._analytics_outbox

    @staticmethod
    def benchmark_sample(generation: Generation) -> ProviderBenchmarkSample | None:
        """The loss-tolerant benchmark a settled generation records, if any.

        Single source for the post-commit write below and for the one-commit
        settle, which records the same sample inside its money commit.
        """
        if generation.app == "TrustedRouter Synthetic":
            return None
        return ProviderBenchmarkSample.from_generation(generation)

    def post_commit_analytics(self, generation: Generation) -> None:
        """Record loss-tolerant analytics without affecting settlement success."""
        sample = self.benchmark_sample(generation)
        if sample is not None:
            self.record_benchmark(sample)

    def post_commit_analytics_safely(self, generation: Generation) -> None:
        """Executor boundary for optional post-settle writes.

        The benchmark outbox uses the store's twenty-second Spanner
        transaction/RPC budget. Normal failures retain their individual logs
        and replay paths. Catch unexpected errors too and retain the
        task-specific failure log.
        """
        try:
            self.post_commit_analytics(generation)
        except Exception:
            log.exception(
                "settle_post_commit_analytics_failed generation_id=%s workspace_id=%s "
                "repairable_via=%s",
                generation.id,
                generation.workspace_id,
                "provider analytics outbox replay",
            )

    def _repair_durable_delivery(self, generation: Generation) -> bool:
        outbox = self._operational_analytics_outbox
        if outbox is None:
            return False
        try:
            now = dt.datetime.now(dt.UTC)

            def txn(transaction: Any) -> None:
                if self._generation_records_enabled:
                    if self._param_types is None:
                        raise RuntimeError("Spanner param types are not configured")
                    upsert_generation_record(
                        transaction,
                        self._param_types,
                        generation,
                        terminal_at=now,
                    )
                outbox.enqueue_activity_tx(
                    transaction,
                    generation,
                )

            self._io.database.run_in_transaction(txn)
        except Exception as exc:
            log.exception(
                "spanner.operational_analytics_activity_repair_failed",
                extra={
                    "request_id": generation.request_id,
                    "generation_id": generation.id,
                    "model": generation.model,
                    "provider": generation.provider,
                    "error_class": type(exc).__name__,
                    "error_message": str(exc)[:500],
                    "repairable_via": "settle_outbox",
                },
            )
            return False
        return True

    def get(self, generation_id: str) -> Generation | None:
        if self._generation_records_enabled:
            if self._param_types is None:
                raise RuntimeError("Spanner param types are not configured")
            with self._io.database.snapshot() as snapshot:
                generation = read_generation_record(
                    snapshot,
                    self._param_types,
                    generation_id,
                )
            if generation is not None:
                return generation
        return self._io.read_entity("generation", generation_id, Generation)

    def record_benchmark(self, sample: ProviderBenchmarkSample) -> None:
        if self._analytics_outbox is None:
            return
        try:
            # A separate transaction by construction: analytics is best-effort;
            # money is not. Only the one-commit settle batches this INSERT into
            # its money commit, because that commit falls back to the durable
            # two-commit settle (which lands here) on any failure.
            self._analytics_outbox.enqueue(sample)
        except Exception as exc:
            log.exception(
                "spanner.analytics_outbox_enqueue_failed",
                extra={
                    "event_id": sample.id,
                    "model": sample.model,
                    "provider": sample.provider,
                    "status": sample.status,
                    "error_class": type(exc).__name__,
                    "error_message": str(exc)[:500],
                    "loss_tolerated": True,
                    "repairable_via": "provider analytics outbox replay",
                },
            )

    @overload
    def reconcile_activity(
        self, workspace_id: str, *, date: str | None = None, limit: int = 1000,
        detailed: Literal[False] = False,
    ) -> int: ...

    @overload
    def reconcile_activity(
        self, workspace_id: str | None = None, *, date: str | None = None,
        limit: int = 1000, generation_id: str | None = None,
        detailed: Literal[True], after_id: str | None = None,
    ) -> ActivityReconcileResult: ...

    def reconcile_activity(
        self,
        workspace_id: str | None = None,
        *,
        date: str | None = None,
        limit: int = 1000,
        generation_id: str | None = None,
        detailed: bool = False,
        after_id: str | None = None,
    ) -> int | ActivityReconcileResult:
        """Re-enqueue durable ClickHouse delivery for generations held in Spanner.

        Without a durable outbox nothing is repaired: every scanned generation
        is reported and the repaired count stays zero.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        result = ActivityReconcileResult()
        if generation_id is not None:
            refs = [{"generation_id": generation_id}]
        else:
            if workspace_id is None:
                raise ValueError("workspace_id or generation_id is required")
            prefix = f"{workspace_id}#{date}#" if date is not None else f"{workspace_id}#"
            if detailed:
                # Include the index key so even dangling references can advance
                # the cursor. The generic entity reader returns bodies only.
                refs = self._reconcile_page(prefix, after_id=after_id, limit=limit + 1)
            else:
                refs = self._io.list_entities(
                    "generation_by_workspace", prefix=prefix, cls=dict, limit=limit + 1,
                )
            result.truncated = len(refs) > limit
            refs = refs[:limit]
            if detailed and result.truncated:
                result.next_after_id = refs[-1]["index_id"]
        for ref in refs:
            result.scanned += 1
            generation = self.get(str(ref["generation_id"]))
            if generation is None:
                result.missing.append(str(ref["generation_id"]))
                continue
            if self._operational_analytics_outbox is None:
                continue
            if self._repair_durable_delivery(generation):
                result.durable_repaired += 1
            else:
                result.durable_failed.append(generation.id)
        if detailed:
            return result
        # Preserve the public store's historical integer contract.
        return result.durable_repaired

    def _reconcile_page(
        self, prefix: str, *, after_id: str | None, limit: int,
    ) -> list[dict[str, str]]:
        if self._param_types is None:
            raise RuntimeError("Spanner param types are not configured")
        if after_id is not None and not after_id.startswith(prefix):
            raise ValueError("after_id must belong to the requested workspace/day")
        with self._io.database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                "SELECT id, body FROM tr_entities "
                "WHERE kind=@kind AND STARTS_WITH(id, @prefix) AND id > @after_id "
                "ORDER BY id LIMIT @limit",
                params={"kind": "generation_by_workspace", "prefix": prefix,
                        "after_id": after_id or "", "limit": limit},
                param_types={"kind": self._param_types.STRING,
                             "prefix": self._param_types.STRING,
                             "after_id": self._param_types.STRING,
                             "limit": self._param_types.INT64},
            )
            return [
                {"index_id": row[0], "generation_id": json.loads(row[1])["generation_id"]}
                for row in rows
            ]
