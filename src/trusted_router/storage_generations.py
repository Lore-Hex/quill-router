"""Generation log + per-provider benchmark capture.

Owns the generations dict and provider_benchmarks list. add() rolls the
generation's actual cost into the per-key counters via the
`add_usage_to_key` callable wired in by the parent store, so this module
doesn't need to know how API keys are stored."""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any, Protocol

from trusted_router.storage_activity import (
    filter_generations,
    generation_events,
    generation_metrics,
    summarize_activity,
    usage_bucket_key,
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
        tag_key: str | None = None,
        tag_value: str | None = None,
        group_by_tag: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = filter_generations(
                self.generations.values(),
                workspace_id=workspace_id,
                api_key_hash=api_key_hash,
                date=date,
                tag_key=tag_key,
                tag_value=tag_value,
            )
        return summarize_activity(rows, group_by_tag=group_by_tag)

    def activity_events(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = None,
        date: str | None = None,
        limit: int = 100,
        tag_key: str | None = None,
        tag_value: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = filter_generations(
                self.generations.values(),
                workspace_id=workspace_id,
                api_key_hash=api_key_hash,
                date=date,
                tag_key=tag_key,
                tag_value=tag_value,
            )
        return generation_events(rows, limit=limit)

    def usage_series(
        self,
        workspace_id: str,
        *,
        window_minutes: int,
        granularity: str,
        api_key_hash: str | None = None,
        by_model: bool = False,
    ) -> dict[str, Any]:
        if granularity not in {"minute", "5min", "hour", "day"}:
            raise ValueError("granularity must be 'minute', '5min', 'hour', or 'day'")
        if granularity == "day":
            days = max(1, window_minutes // 1440)
            today = dt.datetime.now(dt.UTC).date()
            start_day = (today - dt.timedelta(days=days - 1)).isoformat()
            end_day = today.isoformat()
            min_created_at = None
        else:
            now = dt.datetime.now(dt.UTC)
            since = now - dt.timedelta(minutes=max(1, window_minutes))
            start_day = since.date().isoformat()
            end_day = now.date().isoformat()
            min_created_at = since.strftime("%Y-%m-%dT%H:%M:%S")
        buckets: dict[str, dict[str, Any]] = {}
        model_buckets: dict[str, dict[str, dict[str, Any]]] = {}
        with self._lock:
            generations = list(self.generations.values())
        for generation in generations:
            day = generation.created_at[:10]
            if generation.workspace_id != workspace_id:
                continue
            if api_key_hash is not None and generation.key_hash != api_key_hash:
                continue
            if day < start_day or day > end_day:
                continue
            if min_created_at is not None and generation.created_at[:19] < min_created_at:
                continue
            bucket = usage_bucket_key(generation.created_at, granularity)
            metrics = generation_metrics(generation)
            _add_usage_metrics(_usage_bucket(buckets, bucket), metrics)
            if by_model:
                _add_usage_metrics(
                    _usage_bucket(model_buckets.setdefault(generation.model, {}), bucket),
                    metrics,
                )
        result: dict[str, Any] = {
            "granularity": granularity,
            "start_day": start_day,
            "end_day": end_day,
            "truncated": False,
            "buckets": _sorted_usage_buckets(buckets),
        }
        if by_model:
            result["by_model"] = {
                model: _sorted_usage_buckets(per_model)
                for model, per_model in sorted(model_buckets.items())
            }
        return result

    def reconcile_activity(
        self,
        workspace_id: str,
        *,
        date: str | None = None,
        limit: int = 1000,
    ) -> int:
        _ = (workspace_id, date, limit)
        return 0


def _usage_bucket(buckets: dict[str, dict[str, Any]], bucket: str) -> dict[str, Any]:
    return buckets.setdefault(
        bucket,
        {
            "bucket": bucket,
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "cost_micro": 0,
            "byok_micro": 0,
        },
    )


def _add_usage_metrics(bucket: dict[str, Any], metrics: dict[str, int]) -> None:
    for key, value in metrics.items():
        bucket[key] += value


def _sorted_usage_buckets(buckets: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [buckets[key] for key in sorted(buckets)]
