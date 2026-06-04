"""Aggregate ProviderBenchmarkSamples into leaderboard statistics.

Pure, store-agnostic aggregation: given a window of samples (organic
production traffic + synthetic rotation-probe samples, combined — the `source`
field is internal-only and intentionally NOT surfaced here), compute per-model
and per-provider performance: p50/p95 TTFT and TTFB, median throughput,
uptime %, error rate, and sample counts.

This is the data layer behind the public ``/leaderboard`` page and the per-model
performance subpages. The page builds it from a recent window of samples behind
the same short cache the status page uses, so there is no per-view store read.
(A future scale optimization can precompute these as Bigtable rollups; the
aggregation here is the reusable core either way.)
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from trusted_router.storage_models import ProviderBenchmarkSample


def _percentile(values: Sequence[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    # Nearest-rank: smallest value at or above the percentile position.
    rank = max(1, -(-percentile * len(ordered) // 100))  # ceil(p*n/100)
    return ordered[min(rank, len(ordered)) - 1]


def _median_float(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


@dataclass
class ProviderModelStats:
    provider: str
    model: str
    sample_count: int = 0
    success_count: int = 0
    error_count: int = 0
    p50_ttft_ms: int | None = None
    p95_ttft_ms: int | None = None
    p50_ttfb_ms: int | None = None
    p95_ttfb_ms: int | None = None
    p50_tokens_per_second: float | None = None
    last_seen: str | None = None

    @property
    def uptime(self) -> float:
        return self.success_count / self.sample_count if self.sample_count else 0.0

    @property
    def error_rate(self) -> float:
        return self.error_count / self.sample_count if self.sample_count else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "sample_count": self.sample_count,
            "uptime": round(self.uptime, 4),
            "error_rate": round(self.error_rate, 4),
            "p50_ttft_ms": self.p50_ttft_ms,
            "p95_ttft_ms": self.p95_ttft_ms,
            "p50_ttfb_ms": self.p50_ttfb_ms,
            "p95_ttfb_ms": self.p95_ttfb_ms,
            "p50_tokens_per_second": (
                round(self.p50_tokens_per_second, 2)
                if self.p50_tokens_per_second is not None
                else None
            ),
            "last_seen": self.last_seen,
        }


@dataclass
class ProviderStats:
    provider: str
    model_count: int = 0
    sample_count: int = 0
    success_count: int = 0
    error_count: int = 0
    p50_ttft_ms: int | None = None
    p50_tokens_per_second: float | None = None

    @property
    def uptime(self) -> float:
        return self.success_count / self.sample_count if self.sample_count else 0.0

    @property
    def error_rate(self) -> float:
        return self.error_count / self.sample_count if self.sample_count else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_count": self.model_count,
            "sample_count": self.sample_count,
            "uptime": round(self.uptime, 4),
            "error_rate": round(self.error_rate, 4),
            "p50_ttft_ms": self.p50_ttft_ms,
            "p50_tokens_per_second": (
                round(self.p50_tokens_per_second, 2)
                if self.p50_tokens_per_second is not None
                else None
            ),
        }


def _sort_key(p50_ttft_ms: int | None) -> tuple[int, int]:
    # Fastest measured TTFT first; un-measured (None) sink to the bottom.
    return (0 if p50_ttft_ms is not None else 1, p50_ttft_ms or 0)


def aggregate_leaderboard(
    samples: Iterable[ProviderBenchmarkSample], *, min_samples: int = 1
) -> dict[str, Any]:
    """Aggregate samples into ranked per-model and per-provider stats.

    Models/providers with fewer than ``min_samples`` are excluded from the
    ranked lists (callers surface a "limited data" note for thin coverage).
    """
    by_model: dict[tuple[str, str], ProviderModelStats] = {}
    ttft: dict[tuple[str, str], list[int]] = {}
    ttfb: dict[tuple[str, str], list[int]] = {}
    tps: dict[tuple[str, str], list[float]] = {}

    for sample in samples:
        key = (sample.provider, sample.model)
        stats = by_model.get(key)
        if stats is None:
            stats = ProviderModelStats(provider=sample.provider, model=sample.model)
            by_model[key] = stats
            ttft[key] = []
            ttfb[key] = []
            tps[key] = []
        stats.sample_count += 1
        if sample.status == "success":
            stats.success_count += 1
        else:
            stats.error_count += 1
        if sample.first_token_milliseconds is not None:
            ttft[key].append(sample.first_token_milliseconds)
        if sample.ttfb_milliseconds is not None:
            ttfb[key].append(sample.ttfb_milliseconds)
        if sample.speed_tokens_per_second:
            tps[key].append(sample.speed_tokens_per_second)
        if stats.last_seen is None or sample.created_at > stats.last_seen:
            stats.last_seen = sample.created_at

    for key, stats in by_model.items():
        stats.p50_ttft_ms = _percentile(ttft[key], 50)
        stats.p95_ttft_ms = _percentile(ttft[key], 95)
        stats.p50_ttfb_ms = _percentile(ttfb[key], 50)
        stats.p95_ttfb_ms = _percentile(ttfb[key], 95)
        stats.p50_tokens_per_second = _median_float(tps[key])

    models = [s for s in by_model.values() if s.sample_count >= min_samples]
    models.sort(key=lambda s: _sort_key(s.p50_ttft_ms))

    providers = _aggregate_providers(models)
    return {
        "models": [s.as_dict() for s in models],
        "providers": [s.as_dict() for s in providers],
        "model_count": len(models),
        "provider_count": len(providers),
        "total_samples": sum(s.sample_count for s in models),
    }


def _aggregate_providers(model_stats: list[ProviderModelStats]) -> list[ProviderStats]:
    by_provider: dict[str, ProviderStats] = {}
    ttft: dict[str, list[int]] = {}
    tps: dict[str, list[float]] = {}
    for stats in model_stats:
        agg = by_provider.get(stats.provider)
        if agg is None:
            agg = ProviderStats(provider=stats.provider)
            by_provider[stats.provider] = agg
            ttft[stats.provider] = []
            tps[stats.provider] = []
        agg.model_count += 1
        agg.sample_count += stats.sample_count
        agg.success_count += stats.success_count
        agg.error_count += stats.error_count
        # Weight each model's p50 by its sample count for the provider median.
        if stats.p50_ttft_ms is not None:
            ttft[stats.provider].extend([stats.p50_ttft_ms] * stats.sample_count)
        if stats.p50_tokens_per_second is not None:
            tps[stats.provider].extend([stats.p50_tokens_per_second] * stats.sample_count)
    providers = list(by_provider.values())
    for agg in providers:
        agg.p50_ttft_ms = _percentile(ttft[agg.provider], 50)
        agg.p50_tokens_per_second = _median_float(tps[agg.provider])
    providers.sort(key=lambda s: _sort_key(s.p50_ttft_ms))
    return providers
