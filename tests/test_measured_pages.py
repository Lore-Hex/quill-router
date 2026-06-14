from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE, ProviderBenchmarkSample


def _settings() -> Settings:
    return Settings(
        environment="test",
        sentry_dsn=None,
        stripe_secret_key=None,
        stripe_webhook_secret=None,
        google_client_id=None,
        google_client_secret=None,
        google_oauth_redirect_url=None,
        github_client_id=None,
        github_client_secret=None,
        github_oauth_redirect_url=None,
    )


def _seed(provider: str, model: str, *, ttft: int, ttfb: int, count: int = 4) -> None:
    for i in range(count):
        STORE.record_provider_benchmark(
            ProviderBenchmarkSample(
                id=f"bench-measured-{provider}-{model}-{i}",
                model=model,
                provider=provider,
                provider_name=provider.title(),
                status="success",
                usage_type="Credits",
                streamed=True,
                first_token_milliseconds=ttft,
                ttfb_milliseconds=ttfb,
                speed_tokens_per_second=120.0,
                source="synthetic",
            )
        )


def test_model_performance_page_shows_measured() -> None:
    client = TestClient(create_app(_settings(), init_observability=False))
    _seed("deepinfra", "meta-llama/llama-3.3-70b-instruct", ttft=150, ttfb=90, count=24)
    resp = client.get("/models/meta-llama/llama-3.3-70b-instruct/performance")
    assert resp.status_code == 200
    body = resp.text
    assert "Measured performance" in body
    assert "deepinfra" in body
    assert "150 ms" in body
    assert '<meta name="robots" content="noindex,follow">' not in body
    assert (
        '<link rel="canonical" href="https://trustedrouter.com/models/meta-llama/llama-3.3-70b-instruct/performance">'
        in body
    )


def test_provider_detail_page_shows_measured() -> None:
    client = TestClient(create_app(_settings(), init_observability=False))
    _seed("cerebras", "meta-llama/llama-3.3-70b-instruct", ttft=110, ttfb=70)
    resp = client.get("/providers/cerebras")
    assert resp.status_code == 200
    body = resp.text
    assert "Measured performance" in body
    assert "110 ms" in body


def test_provider_performance_page_indexes_with_enough_samples() -> None:
    client = TestClient(create_app(_settings(), init_observability=False))
    _seed("cerebras", "meta-llama/llama-3.3-70b-instruct", ttft=110, ttfb=70, count=24)
    resp = client.get("/providers/cerebras/performance")
    assert resp.status_code == 200
    assert "Cerebras performance" in resp.text
    assert "110 ms" in resp.text
    assert '<meta name="robots" content="noindex,follow">' not in resp.text
    assert '<script type="application/ld+json">' in resp.text


def test_model_performance_page_without_data_still_renders() -> None:
    # No seeded samples for this model → no measured table, page still 200.
    client = TestClient(create_app(_settings(), init_observability=False))
    resp = client.get("/models/openai/gpt-5.4-nano/performance")
    assert resp.status_code == 200
    assert '<meta name="robots" content="noindex,follow">' in resp.text
