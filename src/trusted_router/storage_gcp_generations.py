"""Spanner-backed generation log + Bigtable activity index.

Sibling of InMemoryGenerations. add() runs:
  1. add_usage_to_key — roll cost into per-key counters (own txn).
  2. Spanner txn — generation row + workspace index entry.
  3. Bigtable activity-index write (best-effort, repairable from Spanner).
  4. Provider-benchmark sample (Bigtable, best-effort).

The user-facing reads (activity, activity_events) come from the Bigtable
index, not Spanner."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from trusted_router.storage_activity import generation_events, summarize_activity
from trusted_router.storage_gcp_activity_index import (
    activity_generations as _bt_activity_generations,
)
from trusted_router.storage_gcp_activity_index import (
    write_generation as _bt_write_generation,
)
from trusted_router.storage_gcp_benchmark_index import (
    provider_benchmark_samples as _bt_provider_benchmark_samples,
)
from trusted_router.storage_gcp_benchmark_index import (
    write_provider_benchmark as _bt_write_provider_benchmark,
)
from trusted_router.storage_gcp_codec import (
    generation_workspace_id as _generation_workspace_id,
)
from trusted_router.storage_gcp_io import SpannerIO
from trusted_router.storage_models import (
    Generation,
    ProviderBenchmarkSample,
    _is_byok,
)

log = logging.getLogger(__name__)


class _AddUsageCallback(Protocol):
    def __call__(
        self, key_hash: str, cost_microdollars: int, *, is_byok: bool
    ) -> None: ...


class SpannerGenerations:
    def __init__(
        self,
        io: SpannerIO,
        *,
        bt_table: Any,
        generation_family: str,
        add_usage_to_key: _AddUsageCallback,
    ) -> None:
        self._io = io
        self._bt_table = bt_table
        self._family = generation_family
        self._add_usage_to_key = add_usage_to_key

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

    def index_after_commit(self, generation: Generation) -> None:
        # Spanner is the source of truth for billing and generation metadata.
        # Bigtable is the activity index optimized for high-volume reads;
        # this post-transaction write is intentionally idempotent and can
        # be repaired from generation_by_workspace if the process crashes.
        try:
            _bt_write_generation(self._bt_table, self._family, generation)
        except Exception:
            log.exception(
                "bigtable_generation_index_failed",
                extra={"workspace_id": generation.workspace_id, "generation_id": generation.id},
            )
        if generation.app != "TrustedRouter Synthetic":
            self.record_benchmark(ProviderBenchmarkSample.from_generation(generation))

    def get(self, generation_id: str) -> Generation | None:
        return self._io.read_entity("generation", generation_id, Generation)

    def record_benchmark(self, sample: ProviderBenchmarkSample) -> None:
        try:
            _bt_write_provider_benchmark(self._bt_table, self._family, sample)
        except Exception:
            log.exception(
                "bigtable_provider_benchmark_index_failed",
                extra={"model": sample.model, "provider": sample.provider, "status": sample.status},
            )

    def benchmark_samples(
        self,
        *,
        date: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        limit: int = 1000,
    ) -> list[ProviderBenchmarkSample]:
        return _bt_provider_benchmark_samples(
            self._bt_table,
            self._family,
            date=date,
            provider=provider,
            model=model,
            limit=limit,
        )

    def activity(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = None,
        date: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._activity_generations(
            workspace_id, api_key_hash=api_key_hash, date=date, limit=5000
        )
        return summarize_activity(rows)

    def activity_events(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = None,
        date: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = self._activity_generations(
            workspace_id,
            api_key_hash=api_key_hash,
            date=date,
            limit=limit,
        )
        return generation_events(rows)

    def reconcile_activity(
        self,
        workspace_id: str,
        *,
        date: str | None = None,
        limit: int = 1000,
    ) -> int:
        prefix = f"{workspace_id}#{date}#" if date is not None else f"{workspace_id}#"
        refs = self._io.list_entities("generation_by_workspace", prefix=prefix, cls=dict)[:limit]
        repaired = 0
        for ref in refs:
            generation = self.get(str(ref["generation_id"]))
            if generation is None:
                continue
            try:
                _bt_write_generation(self._bt_table, self._family, generation)
                repaired += 1
            except Exception:
                log.exception(
                    "bigtable_generation_index_failed",
                    extra={
                        "workspace_id": generation.workspace_id,
                        "generation_id": generation.id,
                    },
                )
        return repaired

    def _activity_generations(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None,
        date: str | None,
        limit: int,
    ) -> list[Generation]:
        return _bt_activity_generations(
            self._bt_table,
            self._family,
            workspace_id,
            api_key_hash=api_key_hash,
            date=date,
            limit=limit,
        )
