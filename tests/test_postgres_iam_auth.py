from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg_pool import ConnectionPool

import trusted_router.storage_postgres as storage_postgres
from trusted_router.config import Settings
from trusted_router.storage import create_store
from trusted_router.storage_postgres import (
    PostgresStore,
    _aws_dsql_connection_details,
    _aws_dsql_token_provider,
    _IamTokenConnectionPool,
)


def test_iam_pool_refreshes_token_before_every_physical_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = 0
    observed: list[tuple[str, float | None]] = []

    def token_provider() -> str:
        nonlocal generated
        generated += 1
        return f"token-{generated}"

    def fake_connect(self: ConnectionPool[Any], timeout: float | None = None) -> object:
        assert isinstance(self.kwargs, dict)
        observed.append((str(self.kwargs["password"]), timeout))
        return object()

    monkeypatch.setattr(ConnectionPool, "_connect", fake_connect)
    pool = _IamTokenConnectionPool(
        conninfo="postgresql://admin@cluster.example/postgres",
        token_provider=token_provider,
        min_size=0,
        max_size=1,
        open=False,
    )

    pool._connect()
    pool._connect(2.5)

    assert generated == 2
    assert observed == [("token-1", None), ("token-2", 2.5)]


def test_empty_iam_auth_does_not_import_boto3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePool:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def close(self) -> None:
            pass

    monkeypatch.delitem(sys.modules, "boto3", raising=False)
    monkeypatch.setattr(storage_postgres, "ConnectionPool", FakePool)

    store = PostgresStore("postgresql://postgres.example/test")
    try:
        assert "boto3" not in sys.modules
        assert isinstance(store._pool, FakePool)
    finally:
        store.close()


def test_dsql_token_provider_uses_expected_client_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeClient:
        def generate_db_connect_admin_auth_token(self, **kwargs: Any) -> str:
            calls.append(kwargs)
            return f"token-{len(calls)}"

    fake_boto3 = types.ModuleType("boto3")

    def client(service: str, *, region_name: str) -> FakeClient:
        assert service == "dsql"
        assert region_name == "us-west-2"
        return FakeClient()

    fake_boto3.client = client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    provider = _aws_dsql_token_provider(
        "cluster.dsql.us-west-2.on.aws",
        "us-west-2",
    )

    assert provider() == "token-1"
    assert provider() == "token-2"
    assert calls == [
        {
            "Hostname": "cluster.dsql.us-west-2.on.aws",
            "Region": "us-west-2",
            "ExpiresIn": 900,
        },
        {
            "Hostname": "cluster.dsql.us-west-2.on.aws",
            "Region": "us-west-2",
            "ExpiresIn": 900,
        },
    ]


def test_dsql_connection_details_infer_region_and_reject_password() -> None:
    dsn = (
        "postgresql://admin@cluster.dsql.eu-west-1.on.aws/postgres"
        "?sslmode=require"
    )

    assert _aws_dsql_connection_details(dsn) == (
        "cluster.dsql.eu-west-1.on.aws",
        "eu-west-1",
    )
    assert _aws_dsql_connection_details(
        "postgresql://admin@custom.example/postgres",
        region_override="ap-southeast-2",
    ) == ("custom.example", "ap-southeast-2")

    with pytest.raises(ValueError, match="must not contain a password"):
        _aws_dsql_connection_details(
            "postgresql://admin:secret@cluster.dsql.us-east-1.on.aws/postgres"
        )


def test_settings_and_create_store_pass_iam_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeStore:
        def __init__(self, dsn: str, **kwargs: Any) -> None:
            captured["dsn"] = dsn
            captured.update(kwargs)

        def apply_schema(self) -> None:
            captured["schema_applied"] = True

    monkeypatch.setenv("TR_POSTGRES_IAM_AUTH", "aws-dsql")
    monkeypatch.setenv("TR_POSTGRES_IAM_REGION", "us-east-2")
    settings = Settings(_env_file=None)
    assert settings.postgres_iam_auth == "aws-dsql"
    assert settings.postgres_iam_region == "us-east-2"

    monkeypatch.setattr(storage_postgres, "PostgresStore", FakeStore)
    result = create_store(
        SimpleNamespace(
            storage_backend="postgres",
            postgres_dsn="postgresql://admin@cluster.example/postgres",
            postgres_iam_auth="aws-dsql",
            postgres_iam_region="us-east-2",
        )
    )

    assert isinstance(result, FakeStore)
    assert captured == {
        "dsn": "postgresql://admin@cluster.example/postgres",
        "postgres_iam_auth": "aws-dsql",
        "postgres_iam_region": "us-east-2",
        # Absent from the SimpleNamespace above, so this pins the *default*:
        # the operational-analytics outbox stays off unless a deployment turns
            # it on, exactly like the Spanner path.
            "operational_analytics_outbox_enabled": False,
            "max_workspaces_per_owner": 25,
            "trust_qualifying_providers": frozenset({"stripe", "x402"}),
            "trust_tier3_min_days": 30,
            "trust_tier3_min_paid_microdollars": 50_000_000,
            "schema_applied": True,
    }
