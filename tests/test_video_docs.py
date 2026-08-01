from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_video_docs_are_public_discoverable_and_truthful(client: TestClient) -> None:
    response = client.get("/docs/video")
    assert response.status_code == 200
    assert "minimax/hailuo-3" in response.text
    assert "Hailuo 3, also called H3" in response.text
    assert "google/veo-3.1-fast" in response.text
    assert "openai/sora-2-pro" in response.text
    assert "runway/gen-4.5" in response.text
    assert "kling/o3-pro" in response.text
    assert "alibaba/wan-2.7" in response.text
    assert "shengshu/vidu-q3" in response.text
    assert "pixverse/c1" in response.text
    assert "Generate Seedance, Veo, Sora, Runway, Kling, Wan, Vidu, PixVerse" in response.text
    assert "does not pass through OpenRouter" in response.text
    assert "quote plus a 20% TrustedRouter fee" in response.text
    assert "not labeled provider ZDR" in response.text
    assert 'href="/docs/video"' in client.get("/docs").text
    assert "/docs/video" in client.get("/docs/llms.txt").text
    assert "/docs/video" in client.get("/docs/llms-full.txt").text
    assert "<loc>https://trustedrouter.com/docs/video</loc>" in client.get("/sitemap-core.xml").text


def test_standalone_video_guide_uses_video_pricing() -> None:
    guide = Path("docs/video-generation.md").read_text()
    assert "TrustedRouter's 20% video fee" in guide
    assert "TrustedRouter's 5% fee" not in guide
