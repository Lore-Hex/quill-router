"""Day-over-day API-drift detection from provider benchmark samples.

The rotation probe (and organic traffic) continuously record
``ProviderBenchmarkSample`` rows per provider+model. Comparing a recent window
against a committed baseline surfaces *undocumented* upstream API changes that
no changelog would tell us about:

* a model that started 404ing or erroring (deprecated / renamed upstream),
* a brand-new error shape (``error_type`` / ``error_status``) we've never seen,
* a latency cliff (p50 TTFT regressed well past its historical value),
* a model that newly appeared.

The logic here is pure and store-agnostic so it is trivially testable; the
``scripts/detect_provider_drift.py`` CLI wires it to the real store + a
committed baseline JSON.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from trusted_router.storage_models import ProviderBenchmarkSample


def _model_key(provider: str, model: str) -> str:
    return f"{provider}/{model}"


@dataclass
class ModelStats:
    """Aggregated performance for one (provider, model) over a window."""

    provider: str
    model: str
    sample_count: int = 0
    error_count: int = 0
    p50_ttft_ms: int | None = None
    error_types: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return _model_key(self.provider, self.model)

    @property
    def error_rate(self) -> float:
        return self.error_count / self.sample_count if self.sample_count else 0.0

    def to_baseline(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "sample_count": self.sample_count,
            "error_rate": round(self.error_rate, 4),
            "p50_ttft_ms": self.p50_ttft_ms,
            "error_types": sorted(set(self.error_types)),
        }


def _median(values: Sequence[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def aggregate(samples: Iterable[ProviderBenchmarkSample]) -> dict[str, ModelStats]:
    """Group samples by (provider, model) into ``ModelStats``."""
    grouped: dict[str, ModelStats] = {}
    ttfts: dict[str, list[int]] = {}
    for sample in samples:
        key = _model_key(sample.provider, sample.model)
        stats = grouped.get(key)
        if stats is None:
            stats = ModelStats(provider=sample.provider, model=sample.model)
            grouped[key] = stats
            ttfts[key] = []
        stats.sample_count += 1
        if sample.status != "success":
            stats.error_count += 1
            if sample.error_type:
                stats.error_types.append(sample.error_type)
            elif sample.error_status:
                stats.error_types.append(f"http_{sample.error_status}")
        # TTFT only meaningful on successful streamed responses.
        if sample.first_token_milliseconds is not None:
            ttfts[key].append(sample.first_token_milliseconds)
    for key, stats in grouped.items():
        stats.p50_ttft_ms = _median(ttfts[key])
    return grouped


@dataclass
class DriftFinding:
    kind: str  # appeared | error_spike | new_error_type | ttft_regression
    provider: str
    model: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "provider": self.provider,
            "model": self.model,
            "detail": self.detail,
        }


def detect_drift(
    current: dict[str, ModelStats],
    baseline: dict[str, dict[str, Any]],
    *,
    min_samples: int = 5,
    error_rate_jump: float = 0.2,
    ttft_regression_ratio: float = 1.5,
) -> list[DriftFinding]:
    """Compare a current window's aggregates against the committed baseline.

    Only models with at least ``min_samples`` in the current window are judged
    (random rotation makes a thin window noisy). Disappearance is intentionally
    NOT flagged on absence — a deprecated/renamed model instead shows up as an
    ``error_spike`` + ``new_error_type`` (e.g. ``http_404``), which is a far
    less noisy signal than "we happened not to sample it."
    """
    findings: list[DriftFinding] = []
    for key, stats in sorted(current.items()):
        if stats.sample_count < min_samples:
            continue
        prior = baseline.get(key)
        if prior is None:
            findings.append(
                DriftFinding(
                    "appeared",
                    stats.provider,
                    stats.model,
                    f"new model not in baseline ({stats.sample_count} samples)",
                )
            )
            continue
        prior_error_rate = float(prior.get("error_rate") or 0.0)
        if stats.error_rate - prior_error_rate >= error_rate_jump:
            findings.append(
                DriftFinding(
                    "error_spike",
                    stats.provider,
                    stats.model,
                    f"error rate {prior_error_rate:.0%} -> {stats.error_rate:.0%}",
                )
            )
        prior_errors = set(prior.get("error_types") or [])
        new_errors = sorted(set(stats.error_types) - prior_errors)
        if new_errors:
            findings.append(
                DriftFinding(
                    "new_error_type",
                    stats.provider,
                    stats.model,
                    f"unseen error(s): {', '.join(new_errors)}",
                )
            )
        prior_ttft = prior.get("p50_ttft_ms")
        if (
            prior_ttft
            and stats.p50_ttft_ms is not None
            and stats.p50_ttft_ms >= prior_ttft * ttft_regression_ratio
        ):
            findings.append(
                DriftFinding(
                    "ttft_regression",
                    stats.provider,
                    stats.model,
                    f"p50 TTFT {prior_ttft}ms -> {stats.p50_ttft_ms}ms",
                )
            )
    return findings


def baseline_from_stats(current: dict[str, ModelStats]) -> dict[str, dict[str, Any]]:
    """Serialize a current aggregate into the committed baseline shape."""
    return {key: stats.to_baseline() for key, stats in sorted(current.items())}
