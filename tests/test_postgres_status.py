from __future__ import annotations

import os
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE, InMemoryStore, configure_store


def test_status_json_renders_with_postgres_store() -> None:
    dsn = os.environ.get("TR_CONFORMANCE_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set TR_CONFORMANCE_POSTGRES_DSN to exercise the Postgres app")

    app = create_app(
        Settings(
            environment="test",
            storage_backend="postgres",
            postgres_dsn=dsn,
        ),
        init_observability=False,
    )
    postgres_store: Any = cast(Any, STORE).target
    try:
        with TestClient(app) as client:
            response = client.get("/status.json")
        assert response.status_code == 200, response.text
        assert "data" in response.json()
    finally:
        postgres_store.close()
        configure_store(InMemoryStore())
