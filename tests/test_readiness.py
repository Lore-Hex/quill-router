from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE


def test_readiness_checks_billing_store_under_both_paths() -> None:
    app = create_app(
        Settings(environment="test"),
        init_observability=False,
    )
    client = TestClient(app)

    bare = client.get("/ready")
    versioned = client.get("/v1/ready")

    assert bare.status_code == 200
    assert bare.json() == {
        "status": "ready",
        "checks": {"billing_store": "ready"},
    }
    assert versioned.json() == bare.json()


def test_readiness_fails_closed_without_leaking_storage_error(
    monkeypatch,
) -> None:
    app = create_app(
        Settings(environment="test"),
        init_observability=False,
    )
    client = TestClient(app)

    def unavailable() -> None:
        raise RuntimeError("private database hostname")

    monkeypatch.setattr(STORE.in_memory_target, "readiness_check", unavailable)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {
        "status": "not_ready",
        "checks": {"billing_store": "unavailable"},
    }
    assert "hostname" not in response.text


def test_status_host_allows_readiness_endpoint() -> None:
    app = create_app(
        Settings(environment="test"),
        init_observability=False,
    )
    client = TestClient(app)

    response = client.get(
        "/ready",
        headers={"host": "status.trustedrouter.com"},
        follow_redirects=False,
    )

    assert response.status_code == 200
