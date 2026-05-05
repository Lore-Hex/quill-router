"""Generation log + per-provider benchmark capture.

Owns the generations dict and provider_benchmarks list. add() rolls the
generation's actual cost into the per-key counters via the
`add_usage_to_key` callable wired in by the parent store, so this module
doesn't need to know how API keys are stored."""

from __future__ import annotations

import threading
from typing import Any, Protocol

from trusted_router.storage_activity import (
    filter_generations,
    generation_events,
    summarize_activity,
)
from trusted_router.storage_models import (
    Generation,
    ProviderBenchmarkSample,
    _is_byok,
)


class _AddUsageCallback(Protocol):
    def __call__(
        self, key_hash: str, cost_microdollars: int, *, is_byok: bool
    ) -> None: ...


class InMemoryGenerations:
    def __init__(
        self,
        *,
        lock: threading.RLock,
        add_usage_to_key: _AddUsageCallback,
    ) -> None:
        self._lock = lock
        self._add_usage_to_key = add_usage_to_key
        self.generations: dict[str, Generation] = {}
        self.provider_benchmarks: list[ProviderBenchmarkSample] = []

    def reset(self) -> None:
        self.generations.clear()
        self.provider_benchmarks.clear()

    def add(self, generation: Generation) -> None:
        with self._lock:
            self.generations[generation.id] = generation
            if generation.app != "TrustedRouter Synthetic":
                self.provider_benchmarks.append(ProviderBenchmarkSample.from_generation(generation))
        self._add_usage_to_key(
            generation.key_hash,
            generation.total_cost_microdollars,
            is_byok=_is_byok(generation.usage_type),
        )

    def record_benchmark(self, sample: ProviderBenchmarkSample) -> None:
        with self._lock:
            self.provider_benchmarks.append(sample)

    def benchmark_samples(
        self,
        *,
        date: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        limit: int = 1000,
    ) -> list[ProviderBenchmarkSample]:
        with self._lock:
            rows = [
                sample
                for sample in self.provider_benchmarks
                if (date is None or sample.created_at.startswith(date))
                and (provider is None or sample.provider == provider)
                and (model is None or sample.model == model)
            ]
        rows.sort(key=lambda sample: sample.created_at, reverse=True)
        return rows[:limit]

    def get(self, generation_id: str) -> Generation | None:
        with self._lock:
            return self.generations.get(generation_id)

    def activity(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = None,
        date: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = filter_generations(
                self.generations.values(),
                workspace_id=workspace_id,
                api_key_hash=api_key_hash,
                date=date,
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
        with self._lock:
            rows = filter_generations(
                self.generations.values(),
                workspace_id=workspace_id,
                api_key_hash=api_key_hash,
                date=date,
            )
        return generation_events(rows, limit=limit)
