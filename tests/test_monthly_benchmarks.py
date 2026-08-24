from __future__ import annotations

from trusted_router.monthly_benchmarks import MonthlyBenchmarkAccumulator
from trusted_router.storage_models import ProviderBenchmarkSample


def _sample(
    sample_id: str,
    *,
    provider: str = "fast",
    model: str = "model/a",
    status: str = "success",
    error_type: str | None = None,
    source: str = "synthetic",
    ttft: int | None = 100,
    output_tokens: int = 10,
    elapsed: int = 1000,
) -> ProviderBenchmarkSample:
    return ProviderBenchmarkSample(
        id=sample_id,
        model=model,
        provider=provider,
        provider_name=provider.title(),
        status=status,
        usage_type="Credits",
        streamed=True,
        output_tokens=output_tokens,
        elapsed_milliseconds=elapsed,
        first_token_milliseconds=ttft,
        error_type=error_type,
        source=source,
        created_at="2026-07-01T00:00:00Z",
    )


def test_monthly_report_excludes_configuration_noise_and_ranks_reliability() -> None:
    report = MonthlyBenchmarkAccumulator("2026-07")
    for index in range(30):
        report.add(_sample(f"fast-{index}", ttft=50))
        report.add(_sample(f"slow-{index}", provider="slow", ttft=500))
    report.add(
        _sample(
            "unsupported",
            provider="fast",
            status="error",
            error_type="unsupported_model",
        )
    )

    payload = report.report()

    assert payload["row_count"] == 61
    assert payload["overall"]["observation_count"] == 60
    assert payload["overall"]["excluded_count"] == 1
    assert [row["provider"] for row in payload["top_providers"][:2]] == [
        "fast",
        "slow",
    ]


def test_monthly_report_keeps_long_throughput_out_of_availability() -> None:
    report = MonthlyBenchmarkAccumulator("2026-07")
    report.add(_sample("short", source="synthetic", output_tokens=1, elapsed=100))
    report.add(
        _sample(
            "long",
            source="synthetic_throughput",
            output_tokens=200,
            elapsed=2000,
        )
    )

    payload = report.report()["overall"]

    assert payload["observation_count"] == 1
    assert payload["throughput_sample_count"] == 1
    assert payload["p50_tokens_per_second"] == 100.0


def test_monthly_ranking_rewards_reliable_large_samples() -> None:
    report = MonthlyBenchmarkAccumulator("2026-07")
    for index in range(30):
        report.add(_sample(f"tiny-{index}", provider="tiny-perfect"))
    for index in range(1_000):
        report.add(
            _sample(
                f"large-{index}",
                provider="large-reliable",
                status="provider_error" if index == 999 else "success",
            )
        )

    rows = report.report()["top_providers"]

    assert rows[0]["provider"] == "large-reliable"
    assert rows[1]["provider"] == "tiny-perfect"


def test_monthly_public_report_omits_aggregate_spend_and_token_volume() -> None:
    report = MonthlyBenchmarkAccumulator("2026-07")
    report.add(_sample("one"))

    serialized = str(report.report())

    assert "cost_microdollars" not in serialized
    assert "input_tokens" not in serialized
    assert "output_tokens" not in serialized
