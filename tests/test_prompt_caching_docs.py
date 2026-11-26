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
    from decimal import Decimal

    from bs4 import BeautifulSoup

    from trusted_router.pricing import (
        CACHE_WRITE_1H_MULTIPLIER,
        cache_token_prices_microdollars,
    )

    page = BeautifulSoup(client.get("/docs/prompt-caching").text, "html.parser")

    def words(selector: str) -> str:
        paragraph = page.select_one(selector)
        assert paragraph is not None, selector
        return " ".join(paragraph.get_text(" ", strip=True).split())

    # The published five-minute multiple is the one settlement applies on
    # every host that serves Claude models, not only the direct route.
    prompt = 1_000_000
    multiples = {
        Decimal(cache_token_prices_microdollars(provider, prompt)[1]) / prompt
        for provider in ("anthropic", "amazon-bedrock", "google-vertex")
    }
    assert len(multiples) == 1
    five_minute = multiples.pop()
    text = words("#cache-write-pricing")
    assert "A cache write lasts five minutes by default" in text
    assert f"charged at {five_minute.normalize()} times the route's input price" in text
    assert f"one-hour writes are charged at {CACHE_WRITE_1H_MULTIPLIER} times the route's input price" in text
    assert five_minute < CACHE_WRITE_1H_MULTIPLIER
    assert '"cache_control": {"type": "ephemeral", "ttl": "1h"}' in text
    assert "Cache reads are charged at the route's cache read price for either lifetime." in text
    assert "On a model with long-prompt price tiers, the input price is the one for the request's tier." in text

    routes = words("#cache-write-routes")
    assert "served on the direct Anthropic route" in routes
    assert "Amazon Bedrock and Google Vertex routes take only the five-minute lifetime" in routes
    assert "refuse another ttl with a 400 ;" in routes
    assert "with fallbacks enabled the request then moves to the next route" in routes
    assert "a request limited to those routes returns the 400 ." in routes
    assert 'Send "provider": {"only": ["anthropic"]} to keep a one-hour request on the Anthropic route.' in routes

    scope = words("#cache-write-scope")
    assert "cache_control on Chat Completions content parts for Claude models" in scope
    assert "They apply on the GCP API hosts: the default https://api.trustedrouter.com/v1 and the EU regional host." in scope
    assert ("The AWS and Azure hosts do not yet price or check the one-hour lifetime "
            "and charge every cache write at the five-minute rate.") in scope


def test_prompt_caching_docs_are_discoverable(client: TestClient) -> None:
    assert 'href="/docs/prompt-caching"' in client.get("/docs").text
    assert "/docs/prompt-caching" in client.get("/llms.txt").text
    assert "/docs/prompt-caching" in client.get("/docs/llms.txt").text
    assert "/docs/prompt-caching" in client.get("/docs/llms-full.txt").text
    assert (
        "<loc>https://trustedrouter.com/docs/prompt-caching</loc>"
        in client.get("/sitemap-core.xml").text
    )
