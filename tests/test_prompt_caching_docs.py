from __future__ import annotations

from fastapi.testclient import TestClient


def test_prompt_caching_docs_publish_usage_billing_and_privacy_contract(
    client: TestClient,
) -> None:
    response = client.get("/docs/prompt-caching")

    assert response.status_code == 200
    assert "cache_control" in response.text
    assert "usage.prompt_tokens_details.cached_tokens" in response.text
    assert "usage.input_tokens_details.cached_tokens" in response.text
    assert "cache_creation_input_tokens" in response.text
    assert "cache_read_input_tokens" in response.text
    assert "integer microdollar arithmetic" in response.text
    assert "does not keep a second prompt cache" in response.text
    assert 'provider.min_privacy = "zdr"' in response.text


def test_prompt_caching_docs_state_routing_and_retention_limits(client: TestClient) -> None:
    response = client.get("/docs/prompt-caching")

    assert response.status_code == 200
    assert '"only": ["deepseek"]' in response.text
    assert '"allow_fallbacks": False' in response.text
    assert "fallback to another provider can be a cache miss" in response.text
    assert "prompt_cache_retention" in response.text
    assert "501 not_supported_in_alpha" in response.text
    assert "Cache hits are not guaranteed" in response.text


def test_prompt_caching_docs_are_discoverable(client: TestClient) -> None:
    assert 'href="/docs/prompt-caching"' in client.get("/docs").text
    assert 'href="/docs/prompt-caching"' in client.get("/").text
    assert "/docs/prompt-caching" in client.get("/llms.txt").text
    assert "/docs/prompt-caching" in client.get("/docs/llms.txt").text
    assert "/docs/prompt-caching" in client.get("/docs/llms-full.txt").text
    assert (
        "<loc>https://trustedrouter.com/docs/prompt-caching</loc>"
        in client.get("/sitemap-core.xml").text
    )
