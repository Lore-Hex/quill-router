from __future__ import annotations

import datetime as dt
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
    output_tokens: int = 0,
    elapsed_milliseconds: int | None = None,
    error_type: str | None = None,
    error_status: int | None = None,
    error_message: str | None = None,
    source: str = "organic",
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
        output_tokens=output_tokens,
        elapsed_milliseconds=elapsed_milliseconds,
        error_type=error_type,
        error_status=error_status,
        error_message=error_message,
        source=source,
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
    # Short request speed is not a sustained-throughput measurement.
    assert model["p50_tokens_per_second"] is None
    assert model["throughput_sample_count"] == 0
    assert result["total_samples"] == 4


def test_public_metrics_separate_completion_provider_and_capacity_ownership() -> None:
    samples = [
        _sample(provider="p", model="p/model", ttft=100),
        _sample(
            provider="p",
            model="p/model",
            status="error",
            error_type="rate_limit_error",
            error_status=429,
            created_at="2026-06-04T00:00:01Z",
        ),
        _sample(
            provider="p",
            model="p/model",
            status="error",
            error_type="router_error",
            error_status=503,
            created_at="2026-06-04T00:00:02Z",
        ),
    ]

    model = aggregate_leaderboard(samples)["models"][0]

    assert model["completion_rate"] == round(1 / 3, 4)
    assert model["provider_availability"] == 0.5
    assert model["capacity_acceptance_rate"] == 0.5
    assert model["availability_within_deadline"] == 0.5
    assert model["failure_owners"] == {"provider": 1, "trustedrouter": 1}
    assert model["failure_classes"] == {
        "provider_capacity": 1,
        "router_fault": 1,
    }


def test_account_quota_does_not_lower_provider_availability() -> None:
    samples = [
        _sample(provider="p", model="p/model", ttft=100),
        _sample(
            provider="p",
            model="p/model",
            status="error",
            error_type="rate_limit_error",
            error_status=429,
            error_message="account quota exceeded",
            created_at="2026-06-04T00:00:01Z",
        ),
    ]

    model = aggregate_leaderboard(samples)["models"][0]

    assert model["completion_rate"] == 0.5
    assert model["provider_availability"] == 1
    assert model["failure_owners"] == {"trustedrouter": 1}


def test_failed_partial_stream_counts_once_against_first_token_deadline() -> None:
    samples = [
        _sample(provider="p", model="p/model", ttft=100),
        _sample(
            provider="p",
            model="p/model",
            status="error",
            ttft=200,
            error_type="stream_interrupted",
            error_status=502,
            created_at="2026-06-04T00:00:01Z",
        ),
    ]

    model = aggregate_leaderboard(samples)["models"][0]

    assert model["deadline_sample_count"] == 2
    assert model["availability_within_deadline"] == 0.5


def test_success_without_ttft_is_not_counted_as_missed_deadline() -> None:
    samples = [
        _sample(provider="p", model="p/model", ttft=100),
        _sample(
            provider="p",
            model="p/model",
            ttft=None,
            created_at="2026-06-04T00:00:01Z",
        ),
    ]

    model = aggregate_leaderboard(samples)["models"][0]

    assert model["provider_availability"] == 1
    assert model["deadline_sample_count"] == 1
    assert model["availability_within_deadline"] == 1


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


def test_models_and_providers_rank_reliability_before_latency() -> None:
    samples = [
        _sample(provider="reliable", model="reliable/m", ttft=500),
        _sample(
            provider="reliable",
            model="reliable/m",
            ttft=520,
            created_at="2026-06-04T00:00:01Z",
        ),
        _sample(provider="flaky", model="flaky/m", ttft=50),
        _sample(
            provider="flaky",
            model="flaky/m",
            status="error",
            error_type="ReadTimeout",
            created_at="2026-06-04T00:00:01Z",
        ),
    ]

    result = aggregate_leaderboard(samples)

    assert [row["model"] for row in result["models"]] == [
        "reliable/m",
        "flaky/m",
    ]
    assert [row["provider"] for row in result["providers"]] == [
        "reliable",
        "flaky",
    ]
    assert result["models"][0]["uptime"] == 1.0
    assert result["models"][1]["uptime"] == 0.5


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


def test_sustained_throughput_replaces_legacy_speed_without_affecting_uptime() -> None:
    samples = [
        _sample(provider="p", model="p/m", ttft=100, tps=10.0),
        _sample(
            provider="p",
            model="p/m",
            status="error",
            error_type="provider_error",
            error_status=502,
        ),
        _sample(
            provider="p",
            model="p/m",
            tps=400.0,
            source="synthetic_throughput",
        ),
        _sample(
            provider="p",
            model="p/m",
            tps=600.0,
            source="synthetic_throughput",
        ),
        _sample(
            provider="p",
            model="p/m",
            status="error",
            error_type="ReadTimeout",
            source="synthetic_throughput",
        ),
    ]

    result = aggregate_leaderboard(samples)
    model = result["models"][0]
    provider = result["providers"][0]

    assert model["sample_count"] == 2
    assert model["throughput_sample_count"] == 2
    assert model["uptime"] == 0.5
    assert model["p50_tokens_per_second"] == 500.0
    assert provider["sample_count"] == 2
    assert provider["throughput_sample_count"] == 2
    assert provider["uptime"] == 0.5
    assert provider["p50_tokens_per_second"] == 500.0
    assert result["total_samples"] == 2
    assert result["total_throughput_samples"] == 2


def test_sustained_throughput_recomputes_legacy_buffered_rows() -> None:
    samples = [
        _sample(provider="p", model="p/m", ttft=100),
        _sample(
            provider="p",
            model="p/m",
            tps=5000.0,
            output_tokens=200,
            elapsed_milliseconds=10_000,
            source="synthetic_throughput",
        ),
    ]

    result = aggregate_leaderboard(samples)

    assert result["models"][0]["p50_tokens_per_second"] == 20.0
    assert result["providers"][0]["p50_tokens_per_second"] == 20.0


def test_throughput_only_route_is_visible_without_claiming_availability() -> None:
    result = aggregate_leaderboard(
        [
            _sample(
                provider="p",
                model="p/throughput-only",
                tps=4000.0,
                output_tokens=240,
                elapsed_milliseconds=6000,
                source="synthetic_throughput",
            )
        ]
    )

    model = result["models"][0]
    assert model["model"] == "p/throughput-only"
    assert model["sample_count"] == 0
    assert model["uptime"] is None
    assert model["throughput_sample_count"] == 1
    assert model["p50_tokens_per_second"] == 40.0
    assert result["providers"][0]["sample_count"] == 0
    assert result["providers"][0]["uptime"] is None


def test_thin_rows_stay_visible_but_do_not_receive_ranks() -> None:
    samples = [
        _sample(provider="thin", model="thin/fast", ttft=10),
        *[
            _sample(
                provider="qualified",
                model="qualified/model",
                ttft=100 + index,
                created_at=f"2026-06-04T00:00:{index:02d}Z",
            )
            for index in range(10)
        ],
    ]

    result = aggregate_leaderboard(
        samples,
        model_rank_min_samples=10,
        provider_rank_min_samples=10,
        rank_min_ttft_samples=3,
    )

    assert [row["model"] for row in result["models"]] == [
        "qualified/model",
        "thin/fast",
    ]
    assert result["models"][0]["rank"] == 1
    assert result["models"][0]["rank_eligible"] is True
    assert result["models"][1]["rank"] is None
    assert result["models"][1]["rank_eligible"] is False
    assert result["providers"][0]["provider"] == "qualified"
    assert result["providers"][0]["rank"] == 1
    assert result["providers"][1]["provider"] == "thin"
    assert result["providers"][1]["rank"] is None


def test_legacy_short_request_speed_never_creates_zero_sample_throughput() -> None:
    result = aggregate_leaderboard(
        [_sample(provider="p", model="p/m", ttft=50, tps=9999.0)]
    )

    model = result["models"][0]
    provider = result["providers"][0]
    assert model["throughput_sample_count"] == 0
    assert model["p50_tokens_per_second"] is None
    assert provider["throughput_sample_count"] == 0
    assert provider["p50_tokens_per_second"] is None


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


def test_public_benchmark_samples_filters_to_recent_window(monkeypatch) -> None:
    recent = _sample(
        provider="openai",
        model="openai/gpt-4.1-mini",
        ttft=120,
        created_at="2026-06-05T19:10:00Z",
    )
    stale = _sample(
        provider="openai",
        model="openai/o3-mini",
        status="error",
        error_type="empty_stream",
        created_at="2026-06-05T18:30:00Z",
    )

    def fake_provider_benchmark_samples(
        *,
        date: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        limit: int = 1000,
    ) -> list[ProviderBenchmarkSample]:
        del date, provider, model, limit
        return [recent, stale]

    monkeypatch.setattr(
        "trusted_router.benchmark_samples.providers_for_display",
        lambda: (SimpleNamespace(slug="openai"),),
    )
    monkeypatch.setattr(
        "trusted_router.benchmark_samples.STORE",
        SimpleNamespace(provider_benchmark_samples=fake_provider_benchmark_samples),
    )

    rows = public_benchmark_samples(
        limit=10,
        per_provider_limit=10,
        recent_minutes=15,
        now=dt.datetime(2026, 6, 5, 19, 15, tzinfo=dt.UTC),
    )

    assert [row.model for row in rows] == ["openai/gpt-4.1-mini"]


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


def test_aggregate_excludes_router_failures_from_provider_uptime() -> None:
    samples = [
        _sample(provider="openai", model="openai/gpt-5.4-nano", ttft=100),
        _sample(
            provider="openai",
            model="openai/gpt-5.4-nano",
            status="error",
            error_type="router_database_contention",
            error_status=503,
        ),
    ]

    result = aggregate_leaderboard(samples)
    model = result["models"][0]

    assert model["sample_count"] == 1
    assert model["uptime"] == 1.0
    assert model["excluded_count"] == 1
    assert model["top_excluded"] == "router_database_contention"


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


def test_aggregate_excludes_organic_config_failures_by_status() -> None:
    # Parasail-style: organic errors carry the provider's RAW error_type +
    # error_status (not the synthetic classifier's normalized type). A 403
    # "deployment doesn't exist" / 404 model-not-found is a config failure,
    # NOT downtime, so it must be excluded from uptime — while a 502 still
    # counts as a real provider-health failure.
    samples = [
        _sample(provider="parasail", model="deepseek/deepseek-v3.2", ttft=120),
        _sample(provider="parasail", model="deepseek/deepseek-v3.2", ttft=130),
        _sample(
            provider="parasail",
            model="deepseek/deepseek-v3.2",
            status="error",
            error_type="provider_error",
            error_status=403,
        ),
        _sample(
            provider="parasail",
            model="deepseek/deepseek-v3.2",
            status="error",
            error_type="provider_error",
            error_status=404,
        ),
        _sample(
            provider="parasail",
            model="deepseek/deepseek-v3.2",
            status="error",
            error_type="provider_error",
            error_status=502,
        ),
    ]

    result = aggregate_leaderboard(samples)
    model = result["models"][0]

    # 2 success + 1 real 502 downtime = 3 counted; the 403 + 404 are excluded.
    assert model["sample_count"] == 3
    assert model["excluded_count"] == 2
    assert model["uptime"] == round(2 / 3, 4)
    assert model["errors"] == {"provider_error": 1}  # only the 502 counts


def test_aggregate_excludes_organic_not_found_error_type() -> None:
    # A raw model_not_found error_type (even without a 4xx status) is a config
    # miss, not downtime.
    samples = [
        _sample(provider="parasail", model="z-ai/glm-5", ttft=100),
        _sample(
            provider="parasail",
            model="z-ai/glm-5",
            status="error",
            error_type="model_not_found",
            error_status=None,
        ),
    ]
    result = aggregate_leaderboard(samples)
    model = result["models"][0]
    assert model["sample_count"] == 1
    assert model["excluded_count"] == 1
    assert model["uptime"] == 1.0


def test_aggregate_still_counts_real_provider_downtime() -> None:
    # 429s, 5xx, and timeouts are genuine provider-health failures and MUST
    # still count against uptime (not silently excluded by the config filter).
    samples = [
        _sample(provider="parasail", model="x/y", ttft=100),
        _sample(
            provider="parasail",
            model="x/y",
            status="error",
            error_type="rate_limited",
            error_status=429,
        ),
        _sample(
            provider="parasail",
            model="x/y",
            status="error",
            error_type="ttfb_exceeded",
            error_status=None,
        ),
        _sample(
            provider="parasail",
            model="x/y",
            status="error",
            error_type="provider_error",
            error_status=500,
        ),
    ]
    result = aggregate_leaderboard(samples)
    model = result["models"][0]
    assert model["sample_count"] == 4
    assert model["excluded_count"] == 0
    assert model["uptime"] == 0.25
