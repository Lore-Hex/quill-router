from fastapi.testclient import TestClient


def test_batch_docs_publish_drop_in_contract(client: TestClient) -> None:
    response = client.get("/docs/batch")

    assert response.status_code == 200
    assert "POST /api/beta/batches" in response.text
    assert "GET /api/beta/batches/{id}" in response.text
    assert "/v1/chat/completions" in response.text
    assert "/v1/responses" in response.text
    assert "/v1/messages" in response.text
    assert "/v1/embeddings" in response.text
    assert "Keep endpoint and model before requests" in response.text
    assert "There is no blanket batch discount yet" in response.text


def test_batch_docs_disclose_encrypted_retention_boundary(client: TestClient) -> None:
    response = client.get("/docs/batch")

    assert "opts into temporary encrypted storage" in response.text
    assert "automatically deleted after 30 days" in response.text
    assert "Strict EU data-residency batch execution is not included" in response.text
    assert "do not receive plaintext batch content" in response.text
    assert "depends on GCP KMS, Cloud Storage, and the project's IAM administration" in (
        response.text
    )
    assert "not the same zero-retention property" in response.text


def test_batch_docs_are_discoverable(client: TestClient) -> None:
    assert 'href="/docs/batch"' in client.get("/docs").text
    assert 'href="/docs/batch"' in client.get("/").text
    assert "/docs/batch" in client.get("/llms.txt").text
    assert "/docs/batch" in client.get("/docs/llms.txt").text
    assert "/docs/batch" in client.get("/docs/llms-full.txt").text
    assert "<loc>https://trustedrouter.com/docs/batch</loc>" in client.get("/sitemap-core.xml").text


def test_public_privacy_and_legal_pages_scope_batch_retention(client: TestClient) -> None:
    security = client.get("/security")
    assert security.status_code == 200
    assert "Ordinary synchronous and streaming inference does not retain it" in (security.text)
    assert "opt-in Batch API" in security.text
    assert "different trust and retention boundary" in security.text

    privacy = client.get("/privacy")
    assert privacy.status_code == 200
    assert "August 4, 2026" in privacy.text
    assert "Submitting a Batch request is an explicit instruction" in privacy.text
    assert "scheduled for deletion after 30 days" in privacy.text

    legal = client.get("/legal")
    assert "not covered by the default zero-retention posture" in legal.text
    assert "approved its encrypted 30-day retention boundary in writing" in legal.text

    dpa = client.get("/legal/dpa")
    assert "Batch instruction" in dpa.text
    assert "excluded from the default zero-retention commitment" in dpa.text

    baa = client.get("/legal/baa")
    assert "signed BAA amendment" in baa.text
    assert "do not use Batch unless a signed amendment expressly permits it" in baa.text
