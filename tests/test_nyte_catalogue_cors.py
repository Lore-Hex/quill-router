"""Public catalogue reads needed by static NYTE pages."""
from fastapi.testclient import TestClient


def test_exchange_catalogue_reads_are_credential_free_and_cors_enabled(client: TestClient) -> None:
    headers = {"Origin": "https://nytokenexchange.com"}
    count = client.get("/v1/models/count", headers=headers)
    providers = client.get("/v1/providers", headers=headers)
    for response in (count, providers):
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in response.headers
    assert count.json()["data"]["count"] == len(client.get("/v1/models").json()["data"])
    assert all(p["routing_status"] in {"active", "blocked"} for p in providers.json()["data"])
