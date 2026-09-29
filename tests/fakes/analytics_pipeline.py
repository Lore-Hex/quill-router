"""A stand-in for the ClickHouse control reader, fed by the store's own outboxes.

Production delivers provider benchmark samples through ``tr_analytics_outbox``
and synthetic samples through ``tr_operational_analytics_outbox`` to
ClickHouse, and the native Spanner store answers every analytics read from
the ClickHouse control reader. The conformance backends have no ClickHouse,
so this reader answers those reads from the rows the two outboxes hold,
replayed through the in-memory store, which is the reference implementation
of the ordering, limit and filter semantics the conformance suite asserts.

What a green run proves: every sample the store records reaches its durable
outbox with a payload the readers can rebuild, and every read reaches the
reader with its filters intact. What it does not prove: the ClickHouse SQL,
which has its own tests (``tests/test_operational_analytics.py``).
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterable
from typing import Any

from trusted_router import storage_gcp_operational_analytics_outbox as _operational_outbox
from trusted_router.storage import InMemoryStore
from trusted_router.storage_models import (
    ProviderBenchmarkSample,
    SyntheticProbeSample,
    SyntheticRollup,
)

SYNTHETIC_EVENT_KIND: str = _operational_outbox.SYNTHETIC_EVENT_KIND
_SYNTHETIC_FIELDS = {field.name for field in dataclasses.fields(SyntheticProbeSample)}
_BENCHMARK_FIELDS = {field.name for field in dataclasses.fields(ProviderBenchmarkSample)}


def _decode(payload: Any) -> dict[str, Any]:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise TypeError(f"outbox payload is not an object: {type(payload).__name__}")
    return payload


class OutboxAnalyticsReader:
    """Serve the store's analytics reads from its durable outbox rows.

    ``operational_rows`` yields ``(event_kind, event_id, payload)`` for every
    row of ``tr_operational_analytics_outbox``; ``benchmark_rows`` yields
    ``(event_id, payload)`` for every row of ``tr_analytics_outbox``. Both are
    read on every call, so a backend reset between tests needs no bookkeeping.
    A duplicate event id is an at-least-once replay and counts once, exactly
    as the ClickHouse ``ReplacingMergeTree`` tables collapse it.
    """

    def __init__(
        self,
        *,
        operational_rows: Callable[[], Iterable[tuple[str, str, Any]]],
        benchmark_rows: Callable[[], Iterable[tuple[str, Any]]],
    ) -> None:
        self._operational_rows = operational_rows
        self._benchmark_rows = benchmark_rows

    def _replay(self) -> InMemoryStore:
        memory = InMemoryStore()
        seen: set[str] = set()
        for event_kind, event_id, payload in self._operational_rows():
            if event_kind != SYNTHETIC_EVENT_KIND or event_id in seen:
                continue
            seen.add(event_id)
            fields = {k: v for k, v in _decode(payload).items() if k in _SYNTHETIC_FIELDS}
            memory.record_synthetic_probe_sample(SyntheticProbeSample(**fields))
        seen.clear()
        for event_id, payload in self._benchmark_rows():
            if event_id in seen:
                continue
            seen.add(event_id)
            fields = {k: v for k, v in _decode(payload).items() if k in _BENCHMARK_FIELDS}
            memory.record_provider_benchmark(ProviderBenchmarkSample(**fields))
        return memory

    def benchmark_samples(
        self,
        *,
        date: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        limit: int = 1000,
    ) -> list[ProviderBenchmarkSample]:
        return self._replay().provider_benchmark_samples(
            date=date, provider=provider, model=model, limit=limit,
        )

    def synthetic_samples(
        self,
        *,
        date: str | None = None,
        target: str | None = None,
        probe_type: str | None = None,
        monitor_region: str | None = None,
        limit: int = 1000,
    ) -> list[SyntheticProbeSample]:
        return self._replay().synthetic_probe_samples(
            date=date,
            target=target,
            probe_type=probe_type,
            monitor_region=monitor_region,
            limit=limit,
        )

    def synthetic_rollups(self, **_kwargs: Any) -> list[SyntheticRollup]:
        raise NotImplementedError(
            "synthetic rollups are computed by the ClickHouse rollup worker "
            "(clickhouse/rollup_synthetic.py) from ingested samples; this reader "
            "carries samples only"
        )
