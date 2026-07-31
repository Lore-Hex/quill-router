from __future__ import annotations

from fastapi.testclient import TestClient


def test_video_docs_are_public_discoverable_and_truthful(client: TestClient) -> None:
    response = client.get("/docs/video")
    assert response.status_code == 200
    assert "minimax/hailuo-3" in response.text
    assert "Hailuo 3, also called H3" in response.text
    assert "does not pass through OpenRouter" in response.text
    assert "not labeled provider ZDR" in response.text
    assert 'href="/docs/video"' in client.get("/docs").text
    assert "/docs/video" in client.get("/docs/llms.txt").text
    assert "/docs/video" in client.get("/docs/llms-full.txt").text
    assert "<loc>https://trustedrouter.com/docs/video</loc>" in client.get(
        "/sitemap-core.xml"
    ).text
