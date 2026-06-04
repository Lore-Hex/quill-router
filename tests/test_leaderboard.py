from __future__ import annotations

from trusted_router.storage_models import ProviderBenchmarkSample
from trusted_router.synthetic.leaderboard import aggregate_leaderboard


def _sample(
    *,
    provider: str,
    model: str,
    status: str = "success",
    ttft: int | None = None,
    ttfb: int | None = None,
    tps: float | None = None,
    created_at: str = "2026-06-04T00:00:00Z",
) -> ProviderBenchmarkSample:
    return ProviderBenchmarkSample(
        id="b",
        model=model,
        provider=provider,
        provider_name=provider,
        status=status,
        usage_type="Credits",
        streamed=True,
        first_token_milliseconds=ttft,
        ttfb_milliseconds=ttfb,
        speed_tokens_per_second=tps,
        created_at=created_at,
    )


def test_aggregate_computes_per_model_metrics() -> None:
    samples = [
        _sample(provider="cerebras", model="c/m", ttft=100, ttfb=80, tps=300.0),
        _sample(provider="cerebras", model="c/m", ttft=200, ttfb=120, tps=320.0),
        _sample(provider="cerebras", model="c/m", ttft=300, ttfb=160, tps=280.0),
        _sample(provider="cerebras", model="c/m", status="error", ttft=None),
    ]
    result = aggregate_leaderboard(samples)
    model = result["models"][0]
    assert model["provider"] == "cerebras"
    assert model["sample_count"] == 4
    assert model["uptime"] == 0.75
    assert model["error_rate"] == 0.25
    assert model["p50_ttft_ms"] == 200  # median of [100,200,300]
    assert model["p50_ttfb_ms"] == 120
    assert model["p50_tokens_per_second"] == 300.0  # median of [280,300,320]
    assert result["total_samples"] == 4


def test_models_sorted_fastest_first_unmeasured_last() -> None:
    samples = [
        _sample(provider="slow", model="slow/m", ttft=500),
        _sample(provider="fast", model="fast/m", ttft=90),
        # No TTFT measured at all -> should sink below measured models.
        _sample(provider="unknown", model="unknown/m", status="error", ttft=None),
    ]
    result = aggregate_leaderboard(samples)
    ordered = [m["model"] for m in result["models"]]
    assert ordered[0] == "fast/m"
    assert ordered[1] == "slow/m"
    assert ordered[2] == "unknown/m"  # un-measured at the bottom


def test_min_samples_filters_thin_models() -> None:
    samples = [
        _sample(provider="a", model="a/keep", ttft=100),
        _sample(provider="a", model="a/keep", ttft=110),
        _sample(provider="a", model="a/drop", ttft=100),  # only 1 sample
    ]
    result = aggregate_leaderboard(samples, min_samples=2)
    models = [m["model"] for m in result["models"]]
    assert models == ["a/keep"]


def test_provider_rollup_aggregates_models() -> None:
    samples = [
        _sample(provider="p", model="p/m1", ttft=100, tps=200.0),
        _sample(provider="p", model="p/m1", ttft=100, tps=200.0),
        _sample(provider="p", model="p/m2", status="error", ttft=None),
    ]
    result = aggregate_leaderboard(samples)
    assert result["provider_count"] == 1
    provider = result["providers"][0]
    assert provider["provider"] == "p"
    assert provider["model_count"] == 2
    assert provider["sample_count"] == 3
    # 2 success / 3 total.
    assert provider["uptime"] == round(2 / 3, 4)


def test_empty_samples_produce_empty_leaderboard() -> None:
    result = aggregate_leaderboard([])
    assert result["models"] == []
    assert result["providers"] == []
    assert result["total_samples"] == 0
