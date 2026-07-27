from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from trusted_router.config import Settings
from trusted_router.storage_models import ProviderBenchmarkSample
from trusted_router.synthetic import cli as cli_module
from trusted_router.synthetic.probes import (
    SyntheticTarget,
    provider_throughput_probe,
    rotation_candidates,
)
from trusted_router.synthetic.throughput import (
    THROUGHPUT_INTERVAL_SECONDS,
    choose_throughput_target,
    projected_monthly_cost_microdollars,
    throughput_candidates,
)


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def test_top_200_throughput_routes_are_deterministic_and_provider_complete() -> None:
    first = throughput_candidates(limit=200)
    second = throughput_candidates(limit=200)
    pool = rotation_candidates()

    assert first == second
    assert len(first) == 200
    assert len(first) == len(set(first))
    assert {provider for provider, _ in first} == set(pool)
    assert ("anthropic", "anthropic/claude-opus-5") in first
    assert any(model == "moonshotai/kimi-k3" for _, model in first)
    assert any(model == "z-ai/glm-5.2" for _, model in first)
    assert any(model == "google/gemini-3.6-flash" for _, model in first)
    assert all(model in pool[provider] for provider, model in first)


def test_throughput_round_robin_visits_every_route_once_per_cycle() -> None:
    candidates = [("a", "a/1"), ("b", "b/1"), ("c", "c/1")]
    picks = [
        choose_throughput_target(
            candidates,
            now_epoch_seconds=float(slot * 120),
            interval_seconds=120,
        )
        for slot in range(6)
    ]
    assert picks == [*candidates, *candidates]
    assert choose_throughput_target([]) is None
    with pytest.raises(ValueError, match="interval_seconds"):
        choose_throughput_target(candidates, interval_seconds=0)
    assert THROUGHPUT_INTERVAL_SECONDS == 60


def test_top_200_monthly_full_cap_cost_stays_inside_reviewed_budget() -> None:
    candidates = throughput_candidates(limit=200)
    projected = projected_monthly_cost_microdollars(candidates)

    assert projected > 0
    assert projected <= 150_000_000


@pytest.mark.asyncio
async def test_throughput_probe_measures_effective_end_to_end_speed() -> None:
    captured: list[dict[str, Any]] = []
    chunks = [
        b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"benchmark "}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"benchmark "}}]}\n\n',
        (
            b'data: {"choices":[{"delta":{},"finish_reason":"length"}],'
            b'"usage":{"prompt_tokens":19,"completion_tokens":251,'
            b'"total_tokens":270}}\n\n'
        ),
        b"data: [DONE]\n\n",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={
                "x-trustedrouter-provider": "cerebras",
                "x-trustedrouter-served-model": "cerebras/gpt-oss-120b",
            },
            stream=_ChunkStream(chunks),
        )

    clock = iter([0.0, 0.1, 0.2, 0.7, 0.8, 0.9, 1.0])
    target = SyntheticTarget(
        "throughput",
        "https://api.trustedrouter.com/v1",
        "us-central1",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await provider_throughput_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="cerebras",
            model="cerebras/gpt-oss-120b",
            clock=lambda: next(clock),
        )

    assert sample.status == "success"
    assert sample.source == "synthetic_throughput"
    assert sample.input_tokens == 19
    assert sample.output_tokens == 251
    assert sample.first_token_milliseconds == 200
    assert sample.ttfb_milliseconds == 100
    assert sample.elapsed_milliseconds == 1000
    assert sample.speed_tokens_per_second == 251.0
    assert sample.total_cost_microdollars > 0
    assert sample.finish_reason == "length"
    assert captured[0]["max_tokens"] == 512
    assert captured[0]["stream_options"] == {"include_usage": True}
    assert captured[0]["provider"] == {"only": ["cerebras"]}
    assert captured[0]["metadata"]["trustedrouter_probe"] == "throughput"


@pytest.mark.asyncio
async def test_throughput_probe_accepts_buffered_sse_without_inflating_speed() -> None:
    chunks = [
        (
            b'data: {"choices":[{"delta":{"content":"benchmark benchmark"}}]}\n\n'
            b'data: {"choices":[{"delta":{},"finish_reason":"length"}],'
            b'"usage":{"prompt_tokens":20,"completion_tokens":200}}\n\n'
            b"data: [DONE]\n\n"
        )
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "x-trustedrouter-provider": "cerebras",
                "x-trustedrouter-served-model": "cerebras/gpt-oss-120b",
            },
            stream=_ChunkStream(chunks),
        )

    clock = iter([0.0, 0.25, 5.0])
    target = SyntheticTarget(
        "throughput",
        "https://api.trustedrouter.com/v1",
        "us-central1",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await provider_throughput_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="cerebras",
            model="cerebras/gpt-oss-120b",
            clock=lambda: next(clock),
        )

    assert sample.status == "success"
    assert sample.first_token_milliseconds == 250
    assert sample.elapsed_milliseconds == 5000
    assert sample.speed_tokens_per_second == 40.0


@pytest.mark.asyncio
async def test_throughput_probe_rejects_short_noisy_samples() -> None:
    chunks = [
        b'data: {"choices":[{"delta":{"content":"benchmark "}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"benchmark "}}]}\n\n',
        (
            b'data: {"choices":[{"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":12,"completion_tokens":64}}\n\n'
        ),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_ChunkStream(chunks))

    clock = iter([0.0, 0.1, 0.4, 0.5, 0.6])
    target = SyntheticTarget(
        "throughput",
        "https://api.trustedrouter.com/v1",
        "us-central1",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await provider_throughput_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="cerebras",
            model="cerebras/gpt-oss-120b",
            clock=lambda: next(clock),
        )

    assert sample.status == "unsupported"
    assert sample.source == "synthetic_throughput"
    assert sample.error_type == "insufficient_throughput_sample"
    assert sample.input_tokens == 12
    assert sample.output_tokens == 64
    assert sample.speed_tokens_per_second is None


@pytest.mark.asyncio
async def test_throughput_probe_errors_keep_separate_provenance() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"type": "rate_limit", "message": "capacity", "status": 429}},
        )

    target = SyntheticTarget(
        "throughput",
        "https://api.trustedrouter.com/v1",
        "us-central1",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await provider_throughput_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="cerebras",
            model="cerebras/gpt-oss-120b",
        )

    assert sample.status == "error"
    assert sample.source == "synthetic_throughput"
    assert sample.error_type == "rate_limit"
    assert sample.error_status == 429


@pytest.mark.asyncio
async def test_throughput_probe_has_a_total_wall_clock_deadline() -> None:
    class _SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            await asyncio.sleep(1)
            yield b'data: {"choices":[{"delta":{"content":"benchmark"}}]}\n\n'

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_SlowStream())

    target = SyntheticTarget(
        "throughput",
        "https://api.trustedrouter.com/v1",
        "us-central1",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await provider_throughput_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="slow-provider",
            model="slow/model",
            total_timeout_seconds=0.01,
        )

    assert sample.status == "error"
    assert sample.source == "synthetic_throughput"
    assert sample.error_type == "TimeoutError"
    assert sample.error_status is None


@pytest.mark.asyncio
async def test_throughput_pass_runs_in_configured_region_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_probe_pass(**_kwargs: Any) -> list[Any]:
        return []

    async def fake_throughput_pass(**kwargs: Any) -> list[ProviderBenchmarkSample]:
        calls.append(str(kwargs["monitor_region"]))
        return [_benchmark_sample()]

    monkeypatch.setattr(cli_module, "_one_probe_pass", fake_probe_pass)
    monkeypatch.setattr(cli_module, "_throughput_pass", fake_throughput_pass)
    settings = Settings(environment="test", sentry_dsn=None)

    us_samples, us_benchmarks = await cli_module._probe_and_rotation_pass(
        settings=settings,
        monitor_region="us-central1",
        control_plane="https://trustedrouter.com",
        internal_token=None,
        api_key="sk-test",  # noqa: S106 - test placeholder.
        timeout=httpx.Timeout(1),
        rotation_enabled=False,
        rotation_per_pass=0,
        rotation_rng=__import__("random").Random(0),  # noqa: S311
        throughput_enabled=True,
        throughput_region="us-central1",
    )
    eu_samples, eu_benchmarks = await cli_module._probe_and_rotation_pass(
        settings=settings,
        monitor_region="europe-west4",
        control_plane="https://trustedrouter.com",
        internal_token=None,
        api_key="sk-test",  # noqa: S106 - test placeholder.
        timeout=httpx.Timeout(1),
        rotation_enabled=False,
        rotation_per_pass=0,
        rotation_rng=__import__("random").Random(0),  # noqa: S311
        throughput_enabled=True,
        throughput_region="us-central1",
    )

    assert us_samples == []
    assert len(us_benchmarks) == 1
    assert eu_samples == []
    assert eu_benchmarks == []
    assert calls == ["us-central1"]


@pytest.mark.asyncio
async def test_throughput_only_cli_skips_every_health_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark_posts: list[dict[str, Any]] = []

    async def forbidden_probe(**_kwargs: Any) -> list[Any]:
        raise AssertionError("throughput-only mode must not run health probes")

    async def fake_throughput_pass(**_kwargs: Any) -> list[ProviderBenchmarkSample]:
        return [_benchmark_sample()]

    class _Response:
        status_code = 200
        text = '{"data":{"recorded":1}}'

    class _Client:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> _Response:
            assert url.endswith("/v1/internal/synthetic/benchmark")
            benchmark_posts.append(kwargs["json"])
            return _Response()

    settings = Settings(
        environment="test",
        sentry_dsn=None,
        internal_gateway_token="internal",  # noqa: S106 - test placeholder.
        synthetic_monitor_api_key="sk-test",  # noqa: S106 - test placeholder.
    )
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_module, "_one_probe_pass", forbidden_probe)
    monkeypatch.setattr(cli_module, "_rotation_pass", forbidden_probe)
    monkeypatch.setattr(cli_module, "_throughput_pass", fake_throughput_pass)
    monkeypatch.setattr(cli_module.httpx, "AsyncClient", _Client)
    monkeypatch.setenv("TR_SYNTHETIC_MONITOR_REGION", "us-central1")
    monkeypatch.setenv("TR_SYNTHETIC_ROTATION_ENABLED", "true")
    monkeypatch.setenv("TR_SYNTHETIC_THROUGHPUT_ENABLED", "true")
    monkeypatch.setenv("TR_SYNTHETIC_THROUGHPUT_ONLY", "true")
    monkeypatch.setenv("TR_SYNTHETIC_THROUGHPUT_REGION", "us-central1")

    assert await cli_module.run() == 0
    assert len(benchmark_posts) == 1
    assert benchmark_posts[0]["samples"][0]["source"] == "synthetic_throughput"


def _benchmark_sample() -> ProviderBenchmarkSample:
    return ProviderBenchmarkSample(
        id="bench-throughput-test",
        model="cerebras/gpt-oss-120b",
        provider="cerebras",
        provider_name="Cerebras",
        status="success",
        usage_type="Credits",
        streamed=True,
        speed_tokens_per_second=500.0,
        source="synthetic_throughput",
    )
