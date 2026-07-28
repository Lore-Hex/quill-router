"""Aggregate ProviderBenchmarkSamples into leaderboard statistics.

Pure, store-agnostic aggregation: given a window of samples (organic
production traffic + synthetic rotation-probe samples, combined — the `source`
field is internal-only and intentionally NOT surfaced here), compute per-model
and per-provider performance: p50/p95 TTFT and TTFB, median throughput,
pinned route success, throughput completion yield, error rate, and sample
counts. The legacy ``uptime`` response key remains as a compatibility alias.

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
        "route_configuration_error",
    }
)
SAME_MODEL_COMPARISON_LIMIT = 20

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
    New rows separate visible and reasoning tokens; legacy rows carry only
    provider-reported output tokens. Derive the end-to-end rate at read time
    and keep stored speed as a compatibility fallback for partial rows.
    """
    output_tokens = (
        sample.visible_output_tokens
        if sample.requested_output_tokens > 0
        else sample.output_tokens
    )
    if (
        output_tokens > 0
        and sample.elapsed_milliseconds is not None
        and sample.elapsed_milliseconds > 0
    ):
        return output_tokens * 1000 / sample.elapsed_milliseconds
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
    throughput_sample_count: int = 0
    throughput_attempt_count: int = 0
    throughput_error_count: int = 0
    throughput_timeout_count: int = 0
    p50_ttft_ms: int | None = None
    p95_ttft_ms: int | None = None
    p50_ttfb_ms: int | None = None
    p95_ttfb_ms: int | None = None
    p50_tokens_per_second: float | None = None
    last_seen: str | None = None
    errors: Counter[str] = field(default_factory=Counter)
    excluded_reasons: Counter[str] = field(default_factory=Counter)
    throughput_errors: Counter[str] = field(default_factory=Counter)

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

    @property
    def throughput_completion_rate(self) -> float:
        if not self.throughput_attempt_count:
            return 0.0
        return self.throughput_sample_count / self.throughput_attempt_count

    @property
    def throughput_timeout_rate(self) -> float:
        if not self.throughput_attempt_count:
            return 0.0
        return self.throughput_timeout_count / self.throughput_attempt_count

    @property
    def throughput_confidence(self) -> str:
        return _throughput_confidence(
            attempts=self.throughput_attempt_count,
            successes=self.throughput_sample_count,
        )

    @property
    def top_throughput_error(self) -> str | None:
        common = self.throughput_errors.most_common(1)
        return common[0][0] if common else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "sample_count": self.sample_count,
            "route_attempt_count": self.sample_count,
            "uptime": round(self.uptime, 4) if self.sample_count else None,
            "route_success_rate": round(self.uptime, 4) if self.sample_count else None,
            "error_rate": round(self.error_rate, 4),
            "excluded_count": self.excluded_count,
            "throughput_sample_count": self.throughput_sample_count,
            "throughput_success_count": self.throughput_sample_count,
            "throughput_attempt_count": self.throughput_attempt_count,
            "throughput_error_count": self.throughput_error_count,
            "throughput_timeout_count": self.throughput_timeout_count,
            "throughput_completion_rate": (
                round(self.throughput_completion_rate, 4)
                if self.throughput_attempt_count
                else None
            ),
            "throughput_timeout_rate": (
                round(self.throughput_timeout_rate, 4)
                if self.throughput_attempt_count
                else None
            ),
            "throughput_confidence": self.throughput_confidence,
            "top_throughput_error": self.top_throughput_error,
            "throughput_errors": dict(self.throughput_errors),
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
    throughput_sample_count: int = 0
    throughput_attempt_count: int = 0
    throughput_error_count: int = 0
    throughput_timeout_count: int = 0
    p50_ttft_ms: int | None = None
    p50_tokens_per_second: float | None = None
    errors: Counter[str] = field(default_factory=Counter)
    excluded_reasons: Counter[str] = field(default_factory=Counter)
    throughput_errors: Counter[str] = field(default_factory=Counter)

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

    @property
    def throughput_completion_rate(self) -> float:
        if not self.throughput_attempt_count:
            return 0.0
        return self.throughput_sample_count / self.throughput_attempt_count

    @property
    def throughput_timeout_rate(self) -> float:
        if not self.throughput_attempt_count:
            return 0.0
        return self.throughput_timeout_count / self.throughput_attempt_count

    @property
    def throughput_confidence(self) -> str:
        return _throughput_confidence(
            attempts=self.throughput_attempt_count,
            successes=self.throughput_sample_count,
        )

    @property
    def top_throughput_error(self) -> str | None:
        common = self.throughput_errors.most_common(1)
        return common[0][0] if common else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_count": self.model_count,
            "sample_count": self.sample_count,
            "route_attempt_count": self.sample_count,
            "uptime": round(self.uptime, 4) if self.sample_count else None,
            "route_success_rate": round(self.uptime, 4) if self.sample_count else None,
            "error_rate": round(self.error_rate, 4),
            "excluded_count": self.excluded_count,
            "throughput_sample_count": self.throughput_sample_count,
            "throughput_success_count": self.throughput_sample_count,
            "throughput_attempt_count": self.throughput_attempt_count,
            "throughput_error_count": self.throughput_error_count,
            "throughput_timeout_count": self.throughput_timeout_count,
            "throughput_completion_rate": (
                round(self.throughput_completion_rate, 4)
                if self.throughput_attempt_count
                else None
            ),
            "throughput_timeout_rate": (
                round(self.throughput_timeout_rate, 4)
                if self.throughput_attempt_count
                else None
            ),
            "throughput_confidence": self.throughput_confidence,
            "top_throughput_error": self.top_throughput_error,
            "throughput_errors": dict(self.throughput_errors),
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


def _throughput_confidence(*, attempts: int, successes: int) -> str:
    if attempts >= 20 and successes >= 10:
        return "high"
    if attempts >= 5 and successes >= 3:
        return "medium"
    return "low"


def _is_throughput_timeout(error_type: str | None) -> bool:
    normalized = (error_type or "").casefold()
    return "timeout" in normalized or normalized in {
        "ttfb_exceeded",
        "first_token_exceeded",
    }


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
    legacy_tps: dict[tuple[str, str], list[float]] = {}
    sustained_tps: dict[tuple[str, str], list[float]] = {}

    for sample in samples:
        key = (sample.provider, sample.model)
        stats = by_model.get(key)
        if stats is None:
            stats = ProviderModelStats(provider=sample.provider, model=sample.model)
            by_model[key] = stats
            ttft[key] = []
            ttfb[key] = []
            legacy_tps[key] = []
            sustained_tps[key] = []
        if sample.source == "synthetic_throughput":
            stats.throughput_attempt_count += 1
            effective_tps = _effective_throughput(sample)
            if sample.status == "success" and effective_tps is not None:
                sustained_tps[key].append(effective_tps)
                stats.throughput_sample_count += 1
            else:
                throughput_error = sample.error_type or "invalid_throughput_sample"
                stats.throughput_error_count += 1
                stats.throughput_errors[throughput_error] += 1
                if _is_throughput_timeout(sample.error_type):
                    stats.throughput_timeout_count += 1
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
        if sample.speed_tokens_per_second:
            legacy_tps[key].append(sample.speed_tokens_per_second)
        if stats.last_seen is None or sample.created_at > stats.last_seen:
            stats.last_seen = sample.created_at

    for key, stats in by_model.items():
        stats.p50_ttft_ms = _percentile(ttft[key], 50)
        stats.p95_ttft_ms = _percentile(ttft[key], 95)
        stats.p50_ttfb_ms = _percentile(ttfb[key], 50)
        stats.p95_ttfb_ms = _percentile(ttfb[key], 95)
        stats.p50_tokens_per_second = _median_float(sustained_tps[key] or legacy_tps[key])

    models = [
        stats
        for stats in by_model.values()
        if stats.sample_count >= min_samples
        or stats.throughput_attempt_count >= min_samples
    ]
    models.sort(key=lambda s: _sort_key(s.p50_ttft_ms))

    providers = _aggregate_providers(models)
    throughput_attempts = sum(s.throughput_attempt_count for s in models)
    throughput_successes = sum(s.throughput_sample_count for s in models)
    throughput_timeouts = sum(s.throughput_timeout_count for s in models)
    route_attempts = sum(s.sample_count for s in models)
    route_successes = sum(s.success_count for s in models)
    return {
        "models": [s.as_dict() for s in models],
        "providers": [s.as_dict() for s in providers],
        "same_model_comparisons": _same_model_comparisons(models),
        "model_count": len(models),
        "provider_count": len(providers),
        "total_samples": route_attempts,
        "total_route_successes": route_successes,
        "route_success_rate": (
            round(route_successes / route_attempts, 4) if route_attempts else None
        ),
        "total_throughput_samples": throughput_successes,
        "total_throughput_attempts": throughput_attempts,
        "total_throughput_timeouts": throughput_timeouts,
        "throughput_completion_rate": (
            round(throughput_successes / throughput_attempts, 4)
            if throughput_attempts
            else None
        ),
        "throughput_timeout_rate": (
            round(throughput_timeouts / throughput_attempts, 4)
            if throughput_attempts
            else None
        ),
        "throughput_confidence": _throughput_confidence(
            attempts=throughput_attempts,
            successes=throughput_successes,
        ),
        "excluded_samples": sum(s.excluded_count for s in by_model.values()),
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
        agg.excluded_count += stats.excluded_count
        agg.throughput_sample_count += stats.throughput_sample_count
        agg.throughput_attempt_count += stats.throughput_attempt_count
        agg.throughput_error_count += stats.throughput_error_count
        agg.throughput_timeout_count += stats.throughput_timeout_count
        agg.errors.update(stats.errors)
        agg.excluded_reasons.update(stats.excluded_reasons)
        agg.throughput_errors.update(stats.throughput_errors)
        # Weight each model's p50 by its sample count for the provider median.
        if stats.p50_ttft_ms is not None:
            ttft[stats.provider].extend([stats.p50_ttft_ms] * stats.sample_count)
        if stats.p50_tokens_per_second is not None:
            weight = stats.throughput_sample_count or stats.sample_count
            tps[stats.provider].extend([stats.p50_tokens_per_second] * weight)
    providers = list(by_provider.values())
    for agg in providers:
        agg.p50_ttft_ms = _percentile(ttft[agg.provider], 50)
        agg.p50_tokens_per_second = _median_float(tps[agg.provider])
    providers.sort(key=lambda s: _sort_key(s.p50_ttft_ms))
    return providers


def _same_model_comparisons(
    model_stats: list[ProviderModelStats],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[ProviderModelStats]] = {}
    for stats in model_stats:
        grouped.setdefault(stats.model, []).append(stats)

    comparisons: list[dict[str, Any]] = []
    for model, rows in grouped.items():
        providers = {row.provider for row in rows}
        if len(providers) < 2:
            continue
        ordered = sorted(
            rows,
            key=lambda row: (
                -(row.uptime if row.sample_count else -1),
                -row.throughput_completion_rate,
                *_sort_key(row.p50_ttft_ms),
                row.provider,
            ),
        )
        comparisons.append(
            {
                "model": model,
                "provider_count": len(providers),
                "route_attempt_count": sum(row.sample_count for row in rows),
                "throughput_attempt_count": sum(
                    row.throughput_attempt_count for row in rows
                ),
                "rows": [row.as_dict() for row in ordered],
            }
        )
    comparisons.sort(
        key=lambda comparison: (
            -int(comparison["provider_count"]),
            -int(comparison["throughput_attempt_count"]),
            -int(comparison["route_attempt_count"]),
            str(comparison["model"]),
        )
    )
    return comparisons[:SAME_MODEL_COMPARISON_LIMIT]


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
