from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.public import _status_page_html
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


def _seed(provider: str, model: str, ttft: int, ttfb: int) -> None:
    for _ in range(3):
        STORE.record_provider_benchmark(
            ProviderBenchmarkSample(
                id=f"bench-page-{provider}-{model}-{ttft}",
                model=model,
                provider=provider,
                provider_name=provider.title(),
                status="success",
                usage_type="Credits",
                streamed=True,
                first_token_milliseconds=ttft,
                ttfb_milliseconds=ttfb,
                speed_tokens_per_second=250.0,
                source="synthetic",
            )
        )


def _seed_excluded(provider: str, model: str) -> None:
    STORE.record_provider_benchmark(
        ProviderBenchmarkSample(
            id=f"bench-page-excluded-{provider}-{model}",
            model=model,
            provider=provider,
            provider_name=provider.title(),
            status="unsupported",
            usage_type="Credits",
            streamed=True,
            error_type="unsupported_route",
            error_status=400,
            source="synthetic",
        )
    )


def test_leaderboard_page_renders_measurements() -> None:
    client = TestClient(create_app(_settings(), init_observability=False))
    # Seed after app creation: the route reads STORE at request time, and app
    # construction may reset the in-memory store.
    _seed("cerebras", "meta/llama-3.3-70b", ttft=120, ttfb=80)
    resp = client.get("/leaderboard")
    assert resp.status_code == 200
    body = resp.text
    assert "Measured performance" in body  # hero eyebrow
    assert "last 15 minutes" in body
    assert "p50 TTFT" in body  # table header
    assert "cerebras" in body  # seeded provider row
    assert "meta/llama-3.3-70b" in body  # seeded model row


def test_leaderboard_page_separates_config_exclusions_from_errors() -> None:
    client = TestClient(create_app(_settings(), init_observability=False))
    _seed("openai", "openai/o4-mini", ttft=150, ttfb=100)
    _seed_excluded("openai", "openai/o4-mini")

    resp = client.get("/leaderboard")

    assert resp.status_code == 200
    assert "Config excluded" in resp.text
    assert "unsupported_route" in resp.text
    assert "Unsupported route and probe-configuration rows" in resp.text


def test_leaderboard_in_sitemap() -> None:
    client = TestClient(create_app(_settings(), init_observability=False))
    resp = client.get("/sitemap.xml")
    assert resp.status_code == 200
    assert "/leaderboard" in resp.text


def test_status_page_surfaces_upstream_provider_errors() -> None:
    # The rotation-probe error data feeds an informational provider-health
    # section on /status (separate from the router-core SLO). Render the page
    # function directly to bypass the HTTP response cache.
    STORE.record_provider_benchmark(
        ProviderBenchmarkSample(
            id="bench-status-err-1",
            model="meta/some-model",
            provider="cerebras",
            provider_name="Cerebras",
            status="error",
            usage_type="Credits",
            streamed=True,
            error_type="http_404",
            source="synthetic",
        )
    )
    html = _status_page_html(_settings(), host="trustedrouter.com")
    assert "Upstream provider health" in html
    assert "last 15 minutes" in html
    assert "cerebras" in html
    assert "http_404" in html  # the captured error type is surfaced


def test_status_page_separates_config_exclusions_from_provider_errors() -> None:
    _seed("openai", "openai/o4-mini", ttft=150, ttfb=100)
    _seed_excluded("openai", "openai/o4-mini")

    html = _status_page_html(_settings(), host="trustedrouter.com")

    assert "Config excluded" in html
    assert "unsupported_route" in html
    assert "Unsupported route and probe-configuration rows" in html
