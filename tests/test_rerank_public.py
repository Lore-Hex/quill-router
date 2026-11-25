from dataclasses import replace

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tests.fixture_routes import bypass_catalog_caches
from tests.rerank_manifest_fixtures import install_public_rerank_models
from trusted_router import catalog, dashboard
from trusted_router.catalog import MODELS, PROVIDERS, is_publicly_listed
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.openrouter_coverage import coverage_map


@pytest.fixture(autouse=True)
def public_rerank_catalog(monkeypatch, tmp_path):
    # Projections are recomputed from the installed rows, not read from a
    # session cache built on whatever the latest price refresh checked in.
    bypass_catalog_caches(monkeypatch)
    install_public_rerank_models(monkeypatch, tmp_path)


def test_every_voyage_rerank_page_uses_catalog_price_context_and_examples(client):
    models = [m for m in MODELS.values() if m.supports_rerank and m.provider == "voyage"]
    assert models
    for model in models:
        response = client.get(f"/models/{model.id}")
        assert response.status_code == 200
        response_text = response.text
        soup = BeautifulSoup(response_text, "html.parser")
        text = soup.get_text(" ", strip=True)
        assert "reranking model" in text
        assert f"{model.context_length:,}-token context window" in text
        price = dashboard._short_dollars(model.prompt_price_microdollars_per_million_tokens, digits=2)
        assert f"{price} per million input tokens" in text
        assert "Output / 1M" not in text and "output price" not in text.lower()
        assert "/chat/completions" not in response.text
        assert soup.find("a", href="/docs#rerank")
        assert f'"model":"{model.id}"' in text
        assert "/rerank" in text
        prompt = soup.select_one("#md-agent-prompt").get_text()
        assert "/v1/rerank and read the key" in prompt and "OpenAI-compatible client" not in prompt
        answers = " ".join(node.get_text(" ", strip=True) for node in soup.select("main details"))
        assert f"Use {model.id} as the model field with POST /v1/rerank on the GCP API hosts." in answers
        assert "OpenAI-compatible API" not in answers
        assert "/1M per million" not in text
        exact = dashboard._price(model.prompt_price_microdollars_per_million_tokens).removesuffix("/1M")
        assert f"The current Credits price is {exact} per million input tokens." in answers
        for section in ("pricing", "providers", "api"):
            response = client.get(f"/models/{model.id}/{section}")
            assert response.status_code == 200
            section_soup = BeautifulSoup(response.text, "html.parser")
            assert "Output" not in [cell.get_text() for cell in section_soup.select("th")]
            assert "chat.completions.create" not in response.text
            if section in {"pricing", "api"}:
                assert section_soup.find("a", href="/docs#rerank")
            description = section_soup.find("meta", attrs={"name": "description"})["content"]
            assert "OpenAI-compatible" not in description and "streaming" not in description
        assert "Output tokens billed separately" not in response_text
        assert "Prepaid reranking through POST /v1/rerank" in response_text


def test_rerank_listing_matches_embedding_projections(client):
    ids = {m.id for m in MODELS.values() if m.supports_rerank and is_publicly_listed(m)}
    assert ids
    for path in ("/v1/models", "/v1/models?include=endpoints", "/v1/models/picker", "/v1/rerank/models"):
        response = client.get(path)
        assert response.status_code == 200
        rows = {row["id"]: row for row in response.json()["data"]}
        assert ids <= rows.keys()
        if path.endswith("/rerank/models"):
            assert rows.keys() == ids
            assert all(row["trustedrouter"]["supports_rerank"] for row in rows.values())
    assert ids.isdisjoint(row["id"] for row in client.get("/v1/embeddings/models").json()["data"])
    for model_id in ids:
        for path in ("/sitemap-models.xml", "/models", "/docs/llms-full.txt"):
            assert model_id in client.get(path).text
        assert client.get(f"/v1/models/{model_id}/endpoints").json()["data"]
    pairs = dashboard._model_comparison_pairs()
    assert ids <= {m.id for pair in pairs for m in pair}


def test_public_visibility_keeps_provider_opt_in(client, monkeypatch):
    model = next(m for m in MODELS.values() if m.supports_rerank)
    assert is_publicly_listed(model)
    assert client.get("/v1/rerank/models").json()["data"]
    monkeypatch.setitem(PROVIDERS, model.provider, replace(PROVIDERS[model.provider], supports_rerank=False))
    assert client.get("/v1/rerank/models").json() == {"data": []}
    assert not is_publicly_listed(model)
    assert not is_publicly_listed(model, include_internal=True)
    assert not is_publicly_listed(replace(model, provider="missing"))
    assert not is_publicly_listed(replace(model, internal_only=True))


def test_docs_example_reads_current_catalog(client, monkeypatch):
    fixture = replace(next(m for m in MODELS.values() if m.supports_rerank), id="voyage/example-rerank")
    monkeypatch.setattr(dashboard, "MODELS", {fixture.id: fixture})
    text = client.get("/docs").text
    soup = BeautifulSoup(text, "html.parser")
    section = soup.select_one("#rerank")
    assert section is not None
    assert fixture.id in section.get_text()
    preferred = replace(fixture, id="voyage/rerank-2.5")
    monkeypatch.setattr(dashboard, "MODELS", {fixture.id: fixture, preferred.id: preferred})
    example = BeautifulSoup(client.get("/docs").text, "html.parser").select_one("#rerank pre").get_text()
    assert preferred.id in example and fixture.id not in example
    for value in ("GCP API hosts", "api-europe-west4.quillrouter.com", "api-aws.trustedrouter.com",
                  "api-azure.trustedrouter.com", "prepaid", "once per document", "Batch", "confidential-only"):
        assert value in section.get_text()


def test_control_plane_rerank_answers_like_other_gateway_operations():
    settings = Settings(environment="test", service_surface="control").model_copy(update={"environment": "canary"})
    app = create_app(settings, configure_store_arg=False, init_observability=False)
    client = TestClient(app)
    speech = client.post("/v1/audio/speech", json={})
    rerank = client.post("/v1/rerank", json={"model": "voyage/rerank-2.5", "query": "q", "documents": ["d"]})
    assert rerank.status_code == speech.status_code == 501
    assert rerank.json() == speech.json()
    assert rerank.json()["error"]["type"] == "endpoint_not_supported"
    assert coverage_map()["/rerank", "POST"].kind == "real"


def test_rerank_openapi_contract(client):
    operation = client.get("/openapi.json").json()["paths"]["/v1/rerank"]["post"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert {row["url"] for row in operation["servers"]} == {
        "https://api.trustedrouter.com", "https://api-europe-west4.quillrouter.com",
    }
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert schema["required"] == ["model", "query", "documents"]
    fields = schema["properties"]
    assert fields["query"]["minLength"] == 1
    assert fields["documents"]["minItems"] == 1 and fields["documents"]["maxItems"] == 1000
    assert fields["documents"]["items"]["oneOf"][1]["additionalProperties"] is False
    assert fields["return_documents"]["default"] is True
    assert fields["top_n"]["minimum"] == 1
    assert {"user", "session_id", "provider"} <= fields.keys()
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response["required"] == ["id", "model", "provider", "results", "usage"]
    assert response["properties"]["results"]["items"]["required"] == ["index", "relevance_score"]
    assert response["properties"]["usage"]["required"] == ["total_tokens"]


@pytest.mark.parametrize("capability", ["supports_chat", "supports_embeddings"])
def test_rerank_models_keep_distinct_capabilities(capability):
    assert all(not getattr(m, capability) for m in catalog.MODELS.values() if m.supports_rerank)


def test_voyage_provider_hero_keeps_embedding_price_policy(client):
    rows = dashboard._provider_model_rows("voyage", test_mode=True)
    assert any(MODELS[str(row["id"])].supports_rerank for row in rows)
    figures = dashboard._provider_hero_figures(PROVIDERS["voyage"], {}, rows)
    assert not any(label == "Input / 1M from" for label, _ in figures)
    assert client.get("/providers/voyage").status_code == 200
