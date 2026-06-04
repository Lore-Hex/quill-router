from __future__ import annotations

from trusted_router.storage_models import ProviderBenchmarkSample
from trusted_router.synthetic.drift import (
    aggregate,
    baseline_from_stats,
    detect_drift,
)


def _sample(
    *,
    provider: str = "openai",
    model: str = "openai/gpt-5.4-nano",
    status: str = "success",
    ttft: int | None = 100,
    error_type: str | None = None,
    error_status: int | None = None,
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
        error_type=error_type,
        error_status=error_status,
        source="synthetic",
    )


def test_aggregate_counts_errors_and_median_ttft() -> None:
    samples = [
        _sample(ttft=100),
        _sample(ttft=200),
        _sample(ttft=300),
        _sample(status="error", ttft=None, error_status=500),
    ]
    stats = aggregate(samples)["openai/openai/gpt-5.4-nano"]
    assert stats.sample_count == 4
    assert stats.error_count == 1
    assert stats.error_rate == 0.25
    assert stats.p50_ttft_ms == 200  # median of [100,200,300]
    assert stats.error_types == ["http_500"]


def test_detect_drift_flags_error_spike() -> None:
    current = aggregate([_sample(status="error", error_status=503) for _ in range(10)])
    baseline = {"openai/openai/gpt-5.4-nano": {"error_rate": 0.0, "error_types": [], "p50_ttft_ms": 100}}
    findings = detect_drift(current, baseline)
    kinds = {f.kind for f in findings}
    assert "error_spike" in kinds
    assert "new_error_type" in kinds  # http_503 unseen in baseline


def test_detect_drift_flags_ttft_regression() -> None:
    current = aggregate([_sample(ttft=400) for _ in range(10)])
    baseline = {
        "openai/openai/gpt-5.4-nano": {"error_rate": 0.0, "error_types": [], "p50_ttft_ms": 100}
    }
    findings = [f for f in detect_drift(current, baseline) if f.kind == "ttft_regression"]
    assert findings and "100ms -> 400ms" in findings[0].detail


def test_detect_drift_flags_new_model_appearance() -> None:
    current = aggregate([_sample() for _ in range(10)])
    findings = detect_drift(current, baseline={})
    assert [f.kind for f in findings] == ["appeared"]


def test_detect_drift_respects_min_samples() -> None:
    # Only 3 samples — below the default min_samples=5 — so no judgment.
    current = aggregate([_sample(status="error", error_status=500) for _ in range(3)])
    baseline = {"openai/openai/gpt-5.4-nano": {"error_rate": 0.0, "error_types": [], "p50_ttft_ms": 100}}
    assert detect_drift(current, baseline) == []


def test_detect_drift_quiet_when_stable() -> None:
    current = aggregate([_sample(ttft=110) for _ in range(10)])
    baseline = {
        "openai/openai/gpt-5.4-nano": {"error_rate": 0.0, "error_types": [], "p50_ttft_ms": 100}
    }
    assert detect_drift(current, baseline) == []


def test_baseline_round_trips_through_detect() -> None:
    current = aggregate([_sample(ttft=120) for _ in range(10)])
    baseline = baseline_from_stats(current)
    # A baseline minted from the current window should show no drift against it.
    assert detect_drift(current, baseline) == []
    assert baseline["openai/openai/gpt-5.4-nano"]["p50_ttft_ms"] == 120


def test_drift_cli_update_then_check(tmp_path, monkeypatch) -> None:
    import scripts.detect_provider_drift as cli

    baseline_file = tmp_path / "baseline.json"
    monkeypatch.setattr(cli, "BASELINE_PATH", baseline_file)

    healthy = [_sample(ttft=100) for _ in range(10)]
    monkeypatch.setattr(cli, "_recent_samples", lambda limit: healthy)
    # Mint a baseline from a healthy window.
    assert cli.main(["--update-baseline"]) == 0
    assert baseline_file.exists()
    # Same window → no drift → --check passes.
    assert cli.main(["--check"]) == 0

    # Upstream starts erroring → --check fails (exit 1) so an alert can fire.
    broken = [_sample(status="error", error_status=500) for _ in range(10)]
    monkeypatch.setattr(cli, "_recent_samples", lambda limit: broken)
    assert cli.main(["--check"]) == 1
