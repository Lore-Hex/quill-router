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

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from trusted_router.storage_models import ProviderBenchmarkSample

NON_DOWNTIME_ERROR_TYPES = frozenset(
    {
        "unsupported_route",
        "probe_config_error",
        "provider_auth_config",
    }
)

# Organic benchmark samples (ProviderBenchmarkSample.from_provider_error) are
# written with the provider's RAW error_type/error_status and are NOT run
# through the synthetic rotation classifier (_rotation_error_type in
# synthetic/probes.py). A config failure — auth or deployment-missing
# (401/403), or model-not-found (404 / a not-found error_type) — means the
# provider simply does not serve that route on our key; it is NOT provider
# downtime and must not count against uptime. The synthetic path already
# excludes these (status="unsupported" / NON_DOWNTIME_ERROR_TYPES); the sets
# below mirror that for organic traffic so the combined public uptime number
# isn't dragged down by dead routes (e.g. Parasail's 403 "deployment doesn't
# exist"). Genuine provider-health failures — timeouts, 429s, 5xx, empty
# streams — are deliberately NOT listed here, so they still count as downtime.
_CONFIG_FAILURE_STATUSES = frozenset({401, 403, 404})
_NOT_FOUND_ERROR_TYPES = frozenset(
    {
        "model_not_found",
        "model_not_available",
        "not_found",
        "not_supported",
        "unsupported",
        "unsupported_model",
        "unsupported_provider",
        "unsupported_route",
        "provider_auth_config",
        "probe_config_error",
        "bad_request",
        "invalid_request",
        "invalid_request_error",
    }
)


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


def _effective_throughput(sample: ProviderBenchmarkSample) -> float | None:
    """Return a buffering-safe output rate for a long synthetic probe.

    Rows written before the metric correction stored post-first-chunk delivery
    speed, which can be wildly inflated when an upstream buffers SSE events.
    All long-probe rows already carry provider-reported output tokens and total
    elapsed time, so derive the honest end-to-end rate at read time. The stored
    speed remains a compatibility fallback for older tests or partial rows.
    """
    if (
        sample.output_tokens > 0
        and sample.elapsed_milliseconds is not None
        and sample.elapsed_milliseconds > 0
    ):
        return sample.output_tokens * 1000 / sample.elapsed_milliseconds
    if sample.speed_tokens_per_second is not None and sample.speed_tokens_per_second > 0:
        return sample.speed_tokens_per_second
    return None


@dataclass
class ProviderModelStats:
    provider: str
    model: str
    sample_count: int = 0
    success_count: int = 0
    error_count: int = 0
    excluded_count: int = 0
    ttft_sample_count: int = 0
    throughput_sample_count: int = 0
    rank: int | None = None
    rank_eligible: bool = False
    p50_ttft_ms: int | None = None
    p95_ttft_ms: int | None = None
    p50_ttfb_ms: int | None = None
    p95_ttfb_ms: int | None = None
    p50_tokens_per_second: float | None = None
    last_seen: str | None = None
    errors: Counter[str] = field(default_factory=Counter)
    excluded_reasons: Counter[str] = field(default_factory=Counter)

    @property
    def uptime(self) -> float:
        return self.success_count / self.sample_count if self.sample_count else 0.0

    @property
    def error_rate(self) -> float:
        return self.error_count / self.sample_count if self.sample_count else 0.0

    @property
    def top_error(self) -> str | None:
        common = self.errors.most_common(1)
        return common[0][0] if common else None

    @property
    def top_excluded(self) -> str | None:
        common = self.excluded_reasons.most_common(1)
        return common[0][0] if common else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "sample_count": self.sample_count,
            "uptime": round(self.uptime, 4) if self.sample_count else None,
            "error_rate": round(self.error_rate, 4),
            "excluded_count": self.excluded_count,
            "ttft_sample_count": self.ttft_sample_count,
            "throughput_sample_count": self.throughput_sample_count,
            "rank": self.rank,
            "rank_eligible": self.rank_eligible,
            "top_error": self.top_error,
            "top_excluded": self.top_excluded,
            "errors": dict(self.errors),
            "excluded_reasons": dict(self.excluded_reasons),
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
    excluded_count: int = 0
    ttft_sample_count: int = 0
    throughput_sample_count: int = 0
    rank: int | None = None
    rank_eligible: bool = False
    p50_ttft_ms: int | None = None
    p50_tokens_per_second: float | None = None
    errors: Counter[str] = field(default_factory=Counter)
    excluded_reasons: Counter[str] = field(default_factory=Counter)

    @property
    def uptime(self) -> float:
        return self.success_count / self.sample_count if self.sample_count else 0.0

    @property
    def error_rate(self) -> float:
        return self.error_count / self.sample_count if self.sample_count else 0.0

    @property
    def top_error(self) -> str | None:
        common = self.errors.most_common(1)
        return common[0][0] if common else None

    @property
    def top_excluded(self) -> str | None:
        common = self.excluded_reasons.most_common(1)
        return common[0][0] if common else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_count": self.model_count,
            "sample_count": self.sample_count,
            "uptime": round(self.uptime, 4) if self.sample_count else None,
            "error_rate": round(self.error_rate, 4),
            "excluded_count": self.excluded_count,
            "ttft_sample_count": self.ttft_sample_count,
            "throughput_sample_count": self.throughput_sample_count,
            "rank": self.rank,
            "rank_eligible": self.rank_eligible,
            "top_error": self.top_error,
            "top_excluded": self.top_excluded,
            "errors": dict(self.errors),
            "excluded_reasons": dict(self.excluded_reasons),
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
    samples: Iterable[ProviderBenchmarkSample],
    *,
    min_samples: int = 1,
    model_rank_min_samples: int = 1,
    provider_rank_min_samples: int = 1,
    rank_min_ttft_samples: int = 1,
) -> dict[str, Any]:
    """Aggregate samples into ranked per-model and per-provider stats.

    Models/providers with fewer than ``min_samples`` are excluded from the
    ranked lists (callers surface a "limited data" note for thin coverage).
    """
    by_model: dict[tuple[str, str], ProviderModelStats] = {}
    ttft: dict[tuple[str, str], list[int]] = {}
    ttfb: dict[tuple[str, str], list[int]] = {}
    sustained_tps: dict[tuple[str, str], list[float]] = {}

    for sample in samples:
        key = (sample.provider, sample.model)
        stats = by_model.get(key)
        if stats is None:
            stats = ProviderModelStats(provider=sample.provider, model=sample.model)
            by_model[key] = stats
            ttft[key] = []
            ttfb[key] = []
            sustained_tps[key] = []
        if sample.source == "synthetic_throughput":
            effective_tps = _effective_throughput(sample)
            if sample.status == "success" and effective_tps is not None:
                sustained_tps[key].append(effective_tps)
                stats.throughput_sample_count += 1
            if stats.last_seen is None or sample.created_at > stats.last_seen:
                stats.last_seen = sample.created_at
            # Long probes are intentionally excluded from availability and
            # TTFT. The short PONG probe already measures both without making a
            # slow 512-token completion look like provider downtime.
            continue
        label = sample.error_type or (
            f"http_{sample.error_status}" if sample.error_status else "error"
        )
        if _excluded_from_uptime(sample):
            stats.excluded_count += 1
            stats.excluded_reasons[label] += 1
            continue
        stats.sample_count += 1
        if sample.status == "success":
            stats.success_count += 1
        else:
            stats.error_count += 1
            stats.errors[label] += 1
        if sample.first_token_milliseconds is not None:
            ttft[key].append(sample.first_token_milliseconds)
        if sample.ttfb_milliseconds is not None:
            ttfb[key].append(sample.ttfb_milliseconds)
        if stats.last_seen is None or sample.created_at > stats.last_seen:
            stats.last_seen = sample.created_at

    for key, stats in by_model.items():
        stats.ttft_sample_count = len(ttft[key])
        stats.p50_ttft_ms = _percentile(ttft[key], 50)
        stats.p95_ttft_ms = _percentile(ttft[key], 95)
        stats.p50_ttfb_ms = _percentile(ttfb[key], 50)
        stats.p95_ttfb_ms = _percentile(ttfb[key], 95)
        stats.p50_tokens_per_second = _median_float(sustained_tps[key])
        stats.rank_eligible = (
            stats.sample_count >= model_rank_min_samples
            and stats.ttft_sample_count >= rank_min_ttft_samples
        )

    models = [
        stats
        for stats in by_model.values()
        if stats.sample_count >= min_samples or stats.throughput_sample_count >= min_samples
    ]
    models.sort(
        key=lambda stats: (
            0 if stats.rank_eligible else 1,
            *_sort_key(stats.p50_ttft_ms),
            stats.model,
            stats.provider,
        )
    )
    next_rank = 1
    for stats in models:
        if stats.rank_eligible:
            stats.rank = next_rank
            next_rank += 1

    providers = _aggregate_providers(
        models,
        provider_rank_min_samples=provider_rank_min_samples,
        rank_min_ttft_samples=rank_min_ttft_samples,
    )
    return {
        "models": [s.as_dict() for s in models],
        "providers": [s.as_dict() for s in providers],
        "model_count": len(models),
        "provider_count": len(providers),
        "total_samples": sum(s.sample_count for s in models),
        "total_throughput_samples": sum(s.throughput_sample_count for s in models),
        "excluded_samples": sum(s.excluded_count for s in by_model.values()),
    }


def _aggregate_providers(
    model_stats: list[ProviderModelStats],
    *,
    provider_rank_min_samples: int,
    rank_min_ttft_samples: int,
) -> list[ProviderStats]:
    by_provider: dict[str, ProviderStats] = {}
    ttft: dict[str, list[int]] = {}
    tps: dict[str, list[float]] = {}
    for model_stat in model_stats:
        agg = by_provider.get(model_stat.provider)
        if agg is None:
            agg = ProviderStats(provider=model_stat.provider)
            by_provider[model_stat.provider] = agg
            ttft[model_stat.provider] = []
            tps[model_stat.provider] = []
        agg.model_count += 1
        agg.sample_count += model_stat.sample_count
        agg.success_count += model_stat.success_count
        agg.error_count += model_stat.error_count
        agg.excluded_count += model_stat.excluded_count
        agg.ttft_sample_count += model_stat.ttft_sample_count
        agg.throughput_sample_count += model_stat.throughput_sample_count
        agg.errors.update(model_stat.errors)
        agg.excluded_reasons.update(model_stat.excluded_reasons)
        # Weight each model's p50 by its sample count for the provider median.
        if model_stat.p50_ttft_ms is not None:
            ttft[model_stat.provider].extend(
                [model_stat.p50_ttft_ms] * model_stat.sample_count
            )
        if model_stat.p50_tokens_per_second is not None:
            weight = model_stat.throughput_sample_count
            tps[model_stat.provider].extend(
                [model_stat.p50_tokens_per_second] * weight
            )
    providers = list(by_provider.values())
    for agg in providers:
        agg.p50_ttft_ms = _percentile(ttft[agg.provider], 50)
        agg.p50_tokens_per_second = _median_float(tps[agg.provider])
        agg.rank_eligible = (
            agg.sample_count >= provider_rank_min_samples
            and agg.ttft_sample_count >= rank_min_ttft_samples
        )
    providers.sort(
        key=lambda stats: (
            0 if stats.rank_eligible else 1,
            *_sort_key(stats.p50_ttft_ms),
            stats.provider,
        )
    )
    next_rank = 1
    for provider_stat in providers:
        if provider_stat.rank_eligible:
            provider_stat.rank = next_rank
            next_rank += 1
    return providers


def _excluded_from_uptime(sample: ProviderBenchmarkSample) -> bool:
    if sample.status == "unsupported":
        return True
    if sample.error_type in NON_DOWNTIME_ERROR_TYPES:
        return True
    # Organic config failures are normalized here (see the module-level note):
    # auth / deployment-missing / model-not-found are not provider downtime.
    if sample.status == "error":
        if sample.error_status in _CONFIG_FAILURE_STATUSES:
            return True
        if (sample.error_type or "").casefold() in _NOT_FOUND_ERROR_TYPES:
            return True
    return False
