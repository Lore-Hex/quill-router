from fastapi.testclient import TestClient


def test_byok_docs_describe_real_endpoints_and_credentials(client: TestClient) -> None:
    response = client.get("/docs/byok")
    assert response.status_code == 200
    assert '<link rel="canonical" href="https://trustedrouter.com/docs/byok">' in response.text
    for text in (
        "https://trustedrouter.com/v1/byok/providers",
        "https://api.trustedrouter.com/v1/chat/completions",
        "TRUSTEDROUTER_MANAGEMENT_KEY", "TRUSTEDROUTER_API_KEY",
        "POST /v1/byok/providers/{provider}/keys",
        "PUT /v1/byok/providers/{provider}",
        "PATCH /v1/byok/providers/{provider}/keys/{key_id}",
        "curl -X DELETE", "unsupported stubs", "return 404",
        '"usage":"byok"', '"fallback_to_credits":false',
        "On ranked text routes", "before credits within each model",
        "unless the request specifies a provider order",
        "Images, video, embeddings, and decisions keep catalog order and may use credits first",
        'provider.usage: "byok"', "to explicitly restrict the request to BYOK",
    ):
        assert text in response.text


def test_byok_docs_are_discoverable(client: TestClient) -> None:
    for path in ("/docs", "/docs/agent-setup"):
        assert 'href="/docs/byok"' in client.get(path).text
    for path in ("/llms.txt", "/llms-full.txt", "/docs/llms.txt", "/docs/llms-full.txt"):
        assert "/docs/byok" in client.get(path).text
    assert "<loc>https://trustedrouter.com/docs/byok</loc>" in client.get("/sitemap-core.xml").text


def test_sdk_choice_and_official_domains(client: TestClient) -> None:
    for path in ("/docs", "/docs/agent-setup", "/for-developers", "/llms.txt"):
        text = client.get(path).text
        for claim in ("Recommended default", "openai", "https://api.trustedrouter.com/v1",
                      "pip install trustedrouter", "import trustedrouter",
                      "https://github.com/Lore-Hex/trusted-router-py"):
            assert claim in text, (path, claim)
    for path in ("/llms.txt", "/about"):
        text = client.get(path).text
        for claim in ("The TrustedRouter website is trustedrouter.com",
                      "the primary API host is api.trustedrouter.com",
                      "Other official hosts are listed in", "llms.txt", "SDK documentation",
                      "https://github.com/Lore-Hex/trusted-router-py#readme",
                      "trustedrouter.ai is not affiliated with TrustedRouter"):
            assert claim in text, (path, claim)
        assert "only official domains" not in text


def test_homepage_explains_route_privacy_controls(client: TestClient) -> None:
    text = client.get("/").text
    assert "Privacy varies by route" not in text
    for claim in ("provider and retention/privacy tier", "provider.min_privacy",
                  "provider.only", "provider.allow_fallbacks", "/docs/provider-routing"):
        assert claim in text
