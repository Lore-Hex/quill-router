"""Deterministic monthly summaries over privacy-safe benchmark metadata."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from math import sqrt
from typing import Any

from trusted_router.provider_reliability import FailureOwner, classify_provider_failure
from trusted_router.storage_models import ProviderBenchmarkSample

_EXCLUDED_OWNERS = {
    FailureOwner.CONFIGURATION,
    FailureOwner.CUSTOMER,
    FailureOwner.TRUSTEDROUTER,
}


def _percentile(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-percentile * len(ordered) // 100))
    return ordered[min(rank, len(ordered)) - 1]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


@dataclass
class _Metrics:
    observation_count: int = 0
    success_count: int = 0
    excluded_count: int = 0
    provider_attempt_count: int = 0
    provider_success_count: int = 0
    ttft: list[int] = field(default_factory=list)
    ttfb: list[int] = field(default_factory=list)
    throughput: list[float] = field(default_factory=list)
    errors: Counter[str] = field(default_factory=Counter)

    def add(self, sample: ProviderBenchmarkSample) -> None:
        if sample.source == "synthetic_throughput":
            if (
                sample.status == "success"
                and sample.output_tokens > 0
                and sample.elapsed_milliseconds is not None
                and sample.elapsed_milliseconds > 0
            ):
                self.throughput.append(
                    sample.output_tokens * 1000 / sample.elapsed_milliseconds
                )
            return
        attribution = classify_provider_failure(
            status=sample.status,
            error_type=sample.error_type,
            error_status=sample.error_status,
            error_message=sample.error_message,
        )
        if attribution.owner in _EXCLUDED_OWNERS:
            self.excluded_count += 1
            return
        self.observation_count += 1
        if sample.status == "success":
            self.success_count += 1
        else:
            self.errors[
                sample.error_type
                or (f"http_{sample.error_status}" if sample.error_status else "error")
            ] += 1
        if attribution.counts_toward_provider_availability:
            self.provider_attempt_count += 1
            if sample.status == "success":
                self.provider_success_count += 1
        if sample.first_token_milliseconds is not None:
            self.ttft.append(sample.first_token_milliseconds)
        if sample.ttfb_milliseconds is not None:
            self.ttfb.append(sample.ttfb_milliseconds)

    def as_dict(self) -> dict[str, Any]:
        completion_rate = (
            self.success_count / self.observation_count if self.observation_count else None
        )
        availability = (
            self.provider_success_count / self.provider_attempt_count
            if self.provider_attempt_count
            else None
        )
        throughput = _median(self.throughput)
        return {
            "observation_count": self.observation_count,
            "success_count": self.success_count,
            "completion_rate": round(completion_rate, 4)
            if completion_rate is not None
            else None,
            "excluded_count": self.excluded_count,
            "provider_attempt_count": self.provider_attempt_count,
            "provider_availability": round(availability, 4)
            if availability is not None
            else None,
            "ttft_sample_count": len(self.ttft),
            "p50_ttft_ms": _percentile(self.ttft, 50),
            "p95_ttft_ms": _percentile(self.ttft, 95),
            "p50_ttfb_ms": _percentile(self.ttfb, 50),
            "p95_ttfb_ms": _percentile(self.ttfb, 95),
            "throughput_sample_count": len(self.throughput),
            "p50_tokens_per_second": round(throughput, 2)
            if throughput is not None
            else None,
            "top_error": self.errors.most_common(1)[0][0] if self.errors else None,
        }


class MonthlyBenchmarkAccumulator:
    """One-pass monthly aggregation without retaining benchmark rows."""

    def __init__(self, period: str) -> None:
        self.period = period
        self.row_count = 0
        self.sources: Counter[str] = Counter()
        self.overall = _Metrics()
        self.providers: dict[str, _Metrics] = {}
        self.model_routes: dict[tuple[str, str], _Metrics] = {}
        self.models: set[str] = set()

    def add(self, sample: ProviderBenchmarkSample) -> None:
        self.row_count += 1
        self.sources[sample.source] += 1
        self.models.add(sample.model)
        provider = self.providers.setdefault(sample.provider, _Metrics())
        route = self.model_routes.setdefault((sample.provider, sample.model), _Metrics())
        for metrics in (self.overall, provider, route):
            metrics.add(sample)

    def report(self) -> dict[str, Any]:
        provider_rows = [
            {"provider": provider, **metrics.as_dict()}
            for provider, metrics in self.providers.items()
        ]
        model_rows = [
            {"provider": provider, "model": model, **metrics.as_dict()}
            for (provider, model), metrics in self.model_routes.items()
        ]
        provider_rows.sort(key=_rank_key)
        model_rows.sort(key=_rank_key)
        return {
            "period": self.period,
            "row_count": self.row_count,
            "provider_count": len(self.providers),
            "model_count": len(self.models),
            "model_route_count": len(self.model_routes),
            "sources": dict(sorted(self.sources.items())),
            "overall": self.overall.as_dict(),
            "top_providers": [
                row
                for row in provider_rows
                if int(row["observation_count"]) >= 30
                and int(row["ttft_sample_count"]) >= 3
            ][:12],
            "top_model_routes": [
                row
                for row in model_rows
                if int(row["observation_count"]) >= 10
                and int(row["ttft_sample_count"]) >= 3
            ][:20],
        }


def _wilson_lower_bound(successes: int, attempts: int) -> float:
    if attempts <= 0:
        return -1.0
    z = 1.96
    proportion = successes / attempts
    denominator = 1 + z**2 / attempts
    center = proportion + z**2 / (2 * attempts)
    margin = z * sqrt(
        (proportion * (1 - proportion) + z**2 / (4 * attempts)) / attempts
    )
    return (center - margin) / denominator


def _rank_key(row: dict[str, Any]) -> tuple[float, int, str, str]:
    confidence_floor = _wilson_lower_bound(
        int(row.get("success_count") or 0),
        int(row.get("provider_attempt_count") or 0),
    )
    ttft = row.get("p50_ttft_ms")
    return (
        -confidence_floor,
        int(ttft if ttft is not None else 2**31 - 1),
        str(row.get("model") or ""),
        str(row.get("provider") or ""),
    )
