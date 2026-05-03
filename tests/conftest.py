from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE


@pytest.fixture(autouse=True)
def reset_store() -> None:
    STORE.reset()


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        environment="test",
        sentry_dsn=None,
        internal_gateway_token=None,
        stripe_secret_key=None,
        stripe_webhook_secret=None,
        google_client_id=None,
        google_client_secret=None,
        google_oauth_redirect_url=None,
        github_client_id=None,
        github_client_secret=None,
        github_oauth_redirect_url=None,
    )


@pytest.fixture
def client(test_settings: Settings) -> TestClient:
    return TestClient(create_app(test_settings, init_observability=False))


@pytest.fixture
def user_headers() -> dict[str, str]:
    return {"x-trustedrouter-user": "alice@example.com"}


@pytest.fixture
def inference_key(client: TestClient, user_headers: dict[str, str]) -> str:
    resp = client.post("/v1/keys", headers=user_headers, json={"name": "test key"})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["key"])


@pytest.fixture
def inference_headers(inference_key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {inference_key}"}
