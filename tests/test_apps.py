from __future__ import annotations

from trusted_router.apps import aggregate_apps
from trusted_router.storage_models import ProviderBenchmarkSample


def _s(
    app: str,
    *,
    source: str = "organic",
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> ProviderBenchmarkSample:
    return ProviderBenchmarkSample(
        id=f"x-{app}-{source}-{input_tokens}",
        model="m",
        provider="p",
        provider_name="P",
        status="success",
        usage_type="Credits",
        streamed=True,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        app=app,
        source=source,
    )


def test_aggregate_ranks_named_apps_and_buckets_direct() -> None:
    samples = [
        _s("Acme Chat"),
        _s("Acme Chat"),
        _s("Acme Chat"),
        _s("Beta Bot"),
        _s("Beta Bot"),
        _s(""),  # untitled -> Direct
        _s("TrustedRouter Gateway"),  # default -> Direct
        _s("TrustedRouter Synthetic"),  # monitor name -> excluded
        _s("Probe", source="synthetic"),  # synthetic source -> excluded
    ]
    result = aggregate_apps(samples)
    assert [a["name"] for a in result["apps"]] == ["Acme Chat", "Beta Bot"]
    assert result["apps"][0]["requests"] == 3
    assert result["named_app_count"] == 2
    # Only "" + "TrustedRouter Gateway" are Direct; synthetic is dropped entirely.
    assert result["direct_requests"] == 2


def test_aggregate_sums_tokens_and_respects_min_requests() -> None:
    samples = [
        _s("A", input_tokens=10, output_tokens=5),
        _s("A", input_tokens=20, output_tokens=0),
        _s("B", input_tokens=1, output_tokens=1),
    ]
    result = aggregate_apps(samples, min_requests=2)
    assert [a["name"] for a in result["apps"]] == ["A"]  # B filtered (1 < 2)
    assert result["apps"][0]["tokens"] == 35


def test_aggregate_empty() -> None:
    result = aggregate_apps([])
    assert result["apps"] == []
    assert result["named_app_count"] == 0
    assert result["direct_requests"] == 0
