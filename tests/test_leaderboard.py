from __future__ import annotations

from types import SimpleNamespace

from trusted_router.benchmark_samples import public_benchmark_samples
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
    error_type: str | None = None,
    error_status: int | None = None,
    created_at: str = "2026-06-04T00:00:00Z",
) -> ProviderBenchmarkSample:
    return ProviderBenchmarkSample(
        id=f"b-{provider}-{model}-{status}-{created_at}",
        model=model,
        provider=provider,
        provider_name=provider,
        status=status,
        usage_type="Credits",
        streamed=True,
        first_token_milliseconds=ttft,
        ttfb_milliseconds=ttfb,
        speed_tokens_per_second=tps,
        error_type=error_type,
        error_status=error_status,
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


def test_public_benchmark_samples_reads_each_provider(monkeypatch) -> None:
    deepseek = _sample(provider="deepseek", model="deepseek/deepseek-v4-flash", ttft=300)
    openai = _sample(provider="openai", model="openai/gpt-5.4-nano", ttft=120)
    calls: list[tuple[str | None, int]] = []

    def fake_provider_benchmark_samples(
        *,
        date: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        limit: int = 1000,
    ) -> list[ProviderBenchmarkSample]:
        del date, model
        calls.append((provider, limit))
        if provider == "deepseek":
            return [deepseek]
        if provider == "openai":
            return [openai]
        return [deepseek]

    monkeypatch.setattr(
        "trusted_router.benchmark_samples.providers_for_display",
        lambda: (SimpleNamespace(slug="deepseek"), SimpleNamespace(slug="openai")),
    )
    monkeypatch.setattr(
        "trusted_router.benchmark_samples.STORE",
        SimpleNamespace(provider_benchmark_samples=fake_provider_benchmark_samples),
    )

    rows = public_benchmark_samples(limit=10, per_provider_limit=2)

    assert {row.model for row in rows} == {
        "deepseek/deepseek-v4-flash",
        "openai/gpt-5.4-nano",
    }
    assert ("deepseek", 2) in calls
    assert ("openai", 2) in calls


def test_aggregate_tracks_error_types_per_model_and_provider() -> None:
    samples = [
        _sample(provider="cerebras", model="c/m", status="error", error_type="http_404"),
        _sample(provider="cerebras", model="c/m", status="error", error_type="http_404"),
        _sample(provider="cerebras", model="c/m", status="error", error_type="ConnectError"),
        _sample(provider="cerebras", model="c/m", status="success", ttft=100),
    ]
    result = aggregate_leaderboard(samples)
    model = result["models"][0]
    assert model["error_rate"] == round(3 / 4, 4)
    assert model["top_error"] == "http_404"
    assert model["errors"] == {"http_404": 2, "ConnectError": 1}
    provider = result["providers"][0]
    assert provider["top_error"] == "http_404"
    assert provider["errors"]["http_404"] == 2


def test_aggregate_excludes_unsupported_routes_from_uptime() -> None:
    samples = [
        _sample(provider="openai", model="openai/gpt-4.1-mini", ttft=100),
        _sample(
            provider="openai",
            model="openai/gpt-4.1-mini",
            status="unsupported",
            error_type="unsupported_route",
            error_status=400,
        ),
        _sample(
            provider="openai",
            model="openai/gpt-4.1-mini",
            status="error",
            error_type="provider_error",
            error_status=502,
        ),
    ]

    result = aggregate_leaderboard(samples)
    model = result["models"][0]
    provider = result["providers"][0]

    assert model["sample_count"] == 2
    assert model["excluded_count"] == 1
    assert model["uptime"] == 0.5
    assert model["error_rate"] == 0.5
    assert model["top_error"] == "provider_error"
    assert model["top_excluded"] == "unsupported_route"
    assert model["errors"] == {"provider_error": 1}
    assert model["excluded_reasons"] == {"unsupported_route": 1}
    assert provider["sample_count"] == 2
    assert provider["excluded_count"] == 1
    assert provider["top_error"] == "provider_error"
    assert provider["top_excluded"] == "unsupported_route"
    assert result["total_samples"] == 2
    assert result["excluded_samples"] == 1


def test_aggregate_excluded_only_rows_do_not_surface_as_provider_errors() -> None:
    samples = [
        _sample(provider="openai", model="openai/o4-mini", ttft=100),
        _sample(
            provider="openai",
            model="openai/o4-mini",
            status="unsupported",
            error_type="probe_config_error",
            error_status=400,
        ),
        _sample(
            provider="openai",
            model="openai/o4-mini",
            status="error",
            error_type="provider_auth_config",
            error_status=401,
        ),
    ]

    result = aggregate_leaderboard(samples)
    model = result["models"][0]
    provider = result["providers"][0]

    assert model["sample_count"] == 1
    assert model["uptime"] == 1.0
    assert model["error_rate"] == 0.0
    assert model["top_error"] is None
    assert model["top_excluded"] == "probe_config_error"
    assert model["excluded_count"] == 2
    assert provider["sample_count"] == 1
    assert provider["uptime"] == 1.0
    assert provider["error_rate"] == 0.0
    assert provider["top_error"] is None
    assert provider["excluded_count"] == 2
