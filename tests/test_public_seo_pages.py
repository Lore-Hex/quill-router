from __future__ import annotations

from fastapi.testclient import TestClient


def test_robots_and_sitemap_are_public(client: TestClient) -> None:
    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Sitemap: https://trustedrouter.com/sitemap.xml" in robots.text
    assert "Disallow: /console" in robots.text
    assert "Disallow: /v1/" in robots.text

    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert sitemap.headers["content-type"].startswith("application/xml")
    assert "<urlset" in sitemap.text
    assert sitemap.text.count("<url>") >= 4_000
    assert "<loc>https://trustedrouter.com/models/minimax/minimax-m3/benchmarks</loc>" in sitemap.text
    assert "<loc>https://trustedrouter.com/models/minimax/minimax-m3/providers</loc>" in sitemap.text
    assert "<loc>https://trustedrouter.com/models/minimax/minimax-m3/performance</loc>" in sitemap.text
    assert "<loc>https://trustedrouter.com/models/minimax/minimax-m3/pricing</loc>" in sitemap.text
    assert "<loc>https://trustedrouter.com/models/minimax/minimax-m3/uptime</loc>" in sitemap.text
    assert "<loc>https://trustedrouter.com/models/minimax/minimax-m3/api</loc>" in sitemap.text
    assert "<loc>https://trustedrouter.com/providers/minimax</loc>" in sitemap.text
    assert "<loc>https://trustedrouter.com/compare/models/moonshotai/kimi-k2.6/vs/z-ai/glm-5.1</loc>" in sitemap.text
    assert "trustedrouter/monitor" not in sitemap.text
    assert "openrouter.ai" not in sitemap.text


def test_llms_text_files_are_public_and_do_not_leak_secret_material(
    client: TestClient,
) -> None:
    for path in ["/llms.txt", "/docs/llms.txt", "/docs/llms-full.txt"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "TrustedRouter" in response.text
        assert "api.trustedrouter.com/v1" in response.text
        assert "sk-tr-v1-" not in response.text
        assert "BEGIN PRIVATE KEY" not in response.text


def test_public_provider_route_defaults_to_html_for_link_checkers(client: TestClient) -> None:
    response = client.get("/providers")

    assert response.status_code == 200
    assert "<title>Providers | TrustedRouter</title>" in response.text
    assert "Provider transparency" in response.text
    assert "application/json" not in response.headers["content-type"]

    json_response = client.get("/providers", headers={"accept": "application/json"})
    assert json_response.status_code == 200
    assert json_response.headers["content-type"].startswith("application/json")


def test_provider_detail_page_links_served_models(client: TestClient) -> None:
    response = client.get("/providers/minimax")

    assert response.status_code == 200
    assert "<title>MiniMax Models | TrustedRouter</title>" in response.text
    assert "MiniMax M3" in response.text
    assert "/models/minimax/minimax-m3/benchmarks" in response.text
    assert "Policy source" in response.text


def test_model_seo_cluster_pages_are_public_and_not_openrouter_links(
    client: TestClient,
) -> None:
    for section in ["benchmarks", "providers", "performance", "pricing", "uptime", "api"]:
        response = client.get(f"/models/minimax/minimax-m3/{section}")
        assert response.status_code == 200, response.text
        expected_label = "API" if section == "api" else section.title()
        assert f"MiniMax M3 {expected_label}" in response.text
        assert "openrouter.ai" not in response.text.lower()
        assert '<nav class="section-tabs"' in response.text

    benchmarks = client.get("/models/minimax/minimax-m3/benchmarks")
    assert "MiniMax M3 model page" in benchmarks.text
    assert "LMArena leaderboard" in benchmarks.text

    api = client.get("/models/minimax/minimax-m3/api")
    assert 'model="minimax/minimax-m3"' in api.text
    assert 'base_url="https://api.trustedrouter.com/v1"' in api.text


def test_model_comparison_pages_are_public(client: TestClient) -> None:
    response = client.get("/compare/models/moonshotai/kimi-k2.6/vs/z-ai/glm-5.1")

    assert response.status_code == 200
    assert "MoonshotAI: Kimi K2.6 vs Z.ai: GLM 5.1" in response.text
    assert "Compare routes" in response.text
    assert "/models/moonshotai/kimi-k2.6/pricing" in response.text
    assert "/models/z-ai/glm-5.1/providers" in response.text
    assert "openrouter.ai" not in response.text.lower()


def test_benchmarks_and_rankings_pages_link_model_clusters(client: TestClient) -> None:
    for path in ["/benchmarks", "/rankings"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "/models/minimax/minimax-m3/benchmarks" in response.text
        assert "/models/minimax/minimax-m3/performance" in response.text
        assert "/providers/minimax" in response.text
        assert "openrouter.ai" not in response.text.lower()
