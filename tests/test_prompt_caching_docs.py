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


def test_prompt_caching_docs_state_cache_write_prices(client: TestClient) -> None:
    from bs4 import BeautifulSoup

    from trusted_router.pricing import (
        _CACHE_WRITE_PRICE_MULTIPLIER,
        CACHE_WRITE_1H_MULTIPLIER,
    )

    page = BeautifulSoup(client.get("/docs/prompt-caching").text, "html.parser")
    paragraph = page.select_one("#cache-write-pricing")
    assert paragraph is not None
    text = " ".join(paragraph.get_text(" ", strip=True).split())
    # The published multiples are the ones settlement applies.
    assert f"charged at {_CACHE_WRITE_PRICE_MULTIPLIER['anthropic']} times the route's input price" in text
    assert f"one-hour writes are charged at {CACHE_WRITE_1H_MULTIPLIER} times the route's input price" in text
    assert '"cache_control": {"type": "ephemeral", "ttl": "1h"}' in text
    routes = " ".join(paragraph.find_next_sibling("p").get_text(" ", strip=True).split())
    assert "served on the direct Anthropic route" in routes
    assert "Amazon Bedrock and Google Vertex routes take only the five-minute lifetime" in routes
    assert '"provider": {"only": ["anthropic"]}' in routes


def test_prompt_caching_docs_are_discoverable(client: TestClient) -> None:
    assert 'href="/docs/prompt-caching"' in client.get("/docs").text
    assert "/docs/prompt-caching" in client.get("/llms.txt").text
    assert "/docs/prompt-caching" in client.get("/docs/llms.txt").text
    assert "/docs/prompt-caching" in client.get("/docs/llms-full.txt").text
    assert (
        "<loc>https://trustedrouter.com/docs/prompt-caching</loc>"
        in client.get("/sitemap-core.xml").text
    )
