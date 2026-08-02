from __future__ import annotations

import json
import re
from collections.abc import Mapping

from fastapi.testclient import TestClient

from trusted_router.catalog import META_MODEL_IDS, MODEL_ENDPOINTS, MODELS, PROVIDERS
from trusted_router.dashboard import PUBLIC_PAGES
from trusted_router.seo_catalog import seo_catalog_evidence


def test_every_dedicated_seo_page_renders_current_catalog_evidence(
    client: TestClient,
) -> None:
    seo_pages = [
        key for key, page in PUBLIC_PAGES.items() if page.template.startswith("public/seo_")
    ]
    assert seo_pages
    for page_key in seo_pages:
        response = client.get(f"/{page_key}")
        assert response.status_code == 200, page_key
        assert "Live catalog evidence" in response.text, page_key
        assert "configured routes" in response.text, page_key
        assert "/static/provider-logos/" in response.text, page_key


def test_seo_evidence_counts_the_live_public_catalog(client: TestClient) -> None:
    response = client.get("/openai-compatible-llm-api")
    public_model_count = sum(model.id not in META_MODEL_IDS for model in MODELS.values())
    public_model_ids = {model.id for model in MODELS.values() if model.id not in META_MODEL_IDS}
    public_route_count = sum(
        endpoint.model_id in public_model_ids for endpoint in MODEL_ENDPOINTS.values()
    )

    assert response.status_code == 200
    assert f"<strong>{public_model_count}</strong><span>public models</span>" in response.text
    assert f"<strong>{len(PROVIDERS)}</strong><span>providers</span>" in response.text
    assert f"<strong>{public_route_count}</strong><span>configured routes</span>" in response.text


def test_focused_seo_pages_show_relevant_current_models(client: TestClient) -> None:
    kimi = client.get("/kimi-k2-api")
    glm = client.get("/glm-5-api")
    gemini = client.get("/gemini-flash-alternative")

    assert "kimi-k2.7" in kimi.text.lower()
    assert "glm-5.2" in glm.text.lower()
    assert "gemini-3.5-flash" in gemini.text.lower()


def test_visible_featured_models_match_item_list_structured_data(
    client: TestClient,
) -> None:
    response = client.get("/kimi-k2-api")
    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>',
        response.text,
        re.DOTALL,
    )
    assert match is not None
    payload = json.loads(match.group(1))
    graph = payload["@graph"]
    item_list = next(
        node
        for node in graph
        if node.get("@type") == "ItemList"
        and node.get("name") == "Featured TrustedRouter model routes"
    )
    assert item_list["numberOfItems"] == len(item_list["itemListElement"])
    for item in item_list["itemListElement"]:
        assert item["name"] in response.text
        assert item["url"].removeprefix("https://trustedrouter.com") in response.text


def test_seo_measurements_use_cached_snapshot_shape(
    monkeypatch,
) -> None:
    snapshot: dict[str, object] = {
        "generated_at": "2026-08-02T12:00:00Z",
        "total_samples": 37,
        "provider_count": 1,
        "providers": [
            {
                "provider": "minimax",
                "sample_count": 37,
                "provider_availability": 0.99,
                "p50_ttft_ms": 123,
                "p50_tokens_per_second": 88.5,
            }
        ],
        "models": [
            {
                "model": "minimax/minimax-m3",
                "provider": "minimax",
                "sample_count": 37,
                "provider_availability": 0.99,
                "p50_ttft_ms": 123,
                "p50_tokens_per_second": 88.5,
            }
        ],
    }
    monkeypatch.setattr(
        "trusted_router.seo_catalog.measured_snapshot",
        lambda **_kwargs: snapshot,
    )

    evidence = seo_catalog_evidence("minimax-m3-api")
    models = evidence["models"]
    assert isinstance(models, list)
    minimax = next(
        row for row in models if isinstance(row, Mapping) and row.get("id") == "minimax/minimax-m3"
    )
    assert minimax["p50_ttft_ms"] == 123
    assert minimax["p50_tokens_per_second"] == 88.5
    assert minimax["availability"] == 0.99
    assert minimax["sample_count"] == 37
