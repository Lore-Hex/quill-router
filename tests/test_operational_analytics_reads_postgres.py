from __future__ import annotations

import base64
import contextlib
import datetime as dt
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.activity_tags_recorded import (
    START_AT,
    WORKSPACE,
    fixture_generations,
    recorded_handler,
)
from tests.fakes.postgres import sqlite_postgres_conn
from trusted_router import storage_postgres
from trusted_router.auth import Principal, require_management
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import InMemoryStore, configure_store, create_store
from trusted_router.storage_gcp import SpannerStore
from trusted_router.storage_models import Generation, Workspace
from trusted_router.storage_operational_analytics import activity_payload, analytics_surrogate
from trusted_router.storage_postgres import PostgresStore


@pytest.fixture
def postgres_reader(monkeypatch):
    conn = sqlite_postgres_conn(check_same_thread=False)

    class Pool:
        def __init__(self, **kwargs):
            pass

        def connection(self):
            return contextlib.nullcontext(conn)

        def close(self):
            pass

    monkeypatch.setattr(storage_postgres, "ConnectionPool", Pool)

    def make(backend="postgres"):
        settings = Settings(
            _env_file=None,
            environment="test",
            storage_backend="postgres",
            postgres_dsn="postgresql://postgres.test/records",
            record_read_backend=backend,
            operational_analytics_clickhouse_url="http://plane-clickhouse.test:8123",
            operational_analytics_clickhouse_user="tr_control_read",
            operational_analytics_clickhouse_password="test-reader",  # noqa: S106
            operational_analytics_clickhouse_database="default",
        )
        store = create_store(settings, initialize_schema=False)
        assert isinstance(store, PostgresStore)
        return store, settings

    yield make
    conn._raw.close()


@pytest.fixture
def generation():
    return Generation(
        id="gen-reader-1",
        request_id="req-reader-1",
        workspace_id="ws-reader-1",
        key_hash="key-hash-reader-1",
        model="test/model",
        provider="test-provider",
        provider_name="Test Provider",
        app="test",
        tokens_prompt=12,
        tokens_completion=3,
        reasoning_tokens=1,
        total_cost_microdollars=9,
        usage_type="Credits",
        speed_tokens_per_second=7.5,
        finish_reason="stop",
        status="success",
        streamed=True,
        tags={"group": "test"},
        created_at=dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    )


def _transport(settings, generation, requests, *, copies=1):
    def handler(request):
        requests.append(request)
        params = request.url.params
        assert request.url.host == "plane-clickhouse.test"
        assert params["database"] == "default"
        expected_auth = base64.b64encode(
            f"{settings.operational_analytics_clickhouse_user}:"
            f"{settings.operational_analytics_clickhouse_password}".encode()
        ).decode()
        assert request.headers["authorization"] == f"Basic {expected_auth}"
        assert params["param_tenant_id"] == analytics_surrogate("workspace", generation.workspace_id)
        if "param_key_id" in params:
            assert params["param_key_id"] == analytics_surrogate("api-key", generation.key_hash)
        sql = request.content.decode()
        assert "FROM activity_generations FINAL" in sql
        assert "tenant_id = {tenant_id:String}" in sql
        for raw_id in (generation.workspace_id, generation.key_hash):
            assert raw_id not in str(request.url)
            assert raw_id not in str(request.headers)
            assert raw_id not in sql
        row = activity_payload(generation)
        row.pop("workspace_id")
        return httpx.Response(200, json={"data": [row] * copies})

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("method", [
    "activity", "activity_result", "activity_events", "activity_events_result", "usage_series",
])
def test_postgres_record_reads_stay_unimplemented_by_default(postgres_reader, method):
    store, _ = postgres_reader()
    assert store._operational_analytics is None
    kwargs = {"window_minutes": 60, "granularity": "minute"} if method == "usage_series" else {}
    with pytest.raises(NotImplementedError) as error:
        getattr(store, method)("ws-reader-1", **kwargs)
    assert str(error.value) == f"PostgresStore.{method} is not implemented in increment 1"


@pytest.mark.parametrize("backend", ["postgres", "clickhouse"])
@pytest.mark.parametrize("method", ["reconcile_generation_activity", "typed_key_usage"])
def test_record_read_setting_does_not_enable_repair_or_ledger_counters(postgres_reader, backend, method):
    store, _ = postgres_reader(backend)
    with pytest.raises(NotImplementedError) as error:
        getattr(store, method)("test-id")
    assert str(error.value) == f"PostgresStore.{method} is not implemented in increment 1"


@pytest.mark.parametrize("key_filtered", [False, True])
@pytest.mark.parametrize("method", [
    "activity", "activity_result", "activity_events", "activity_events_result", "usage_series",
])
def test_postgres_record_reads_match_gcp_and_bind_surrogates(
    postgres_reader, generation, method, key_filtered,
):
    store, settings = postgres_reader("clickhouse")
    assert store._operational_analytics is not None
    requests: list[httpx.Request] = []
    store._operational_analytics._transport = _transport(settings, generation, requests)
    spanner = SpannerStore.__new__(SpannerStore)
    spanner._operational_analytics = store._operational_analytics
    kwargs: dict[str, Any] = {}
    if key_filtered:
        kwargs["api_key_hash"] = generation.key_hash
    if method == "usage_series":
        kwargs.update(window_minutes=1440, granularity="day", by_model=True)
    else:
        kwargs.update(date=generation.created_at[:10], tag_key="group", tag_value="test")
        if "events" in method:
            kwargs["limit"] = 2
        else:
            kwargs["group_by_tag"] = "group"
    result = getattr(store, method)(generation.workspace_id, **kwargs)
    assert result == getattr(spanner, method)(generation.workspace_id, **kwargs)
    assert len(requests) == 2
    assert requests[0].url == requests[1].url
    assert requests[0].content == requests[1].content
    params = requests[0].url.params
    assert ("param_key_id" in params) is key_filtered
    if method == "usage_series":
        assert params["param_start_at"] == generation.created_at[:10]
        assert params["param_end_at"] == (
            dt.date.fromisoformat(generation.created_at[:10]) + dt.timedelta(days=1)
        ).isoformat()
        assert params["param_limit"] == "200001"
        assert result["buckets"] == [{
            "bucket": generation.created_at[:10],
            "requests": 1,
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "reasoning_tokens": 1,
            "cost_micro": 9,
            "byok_micro": 0,
        }]
        assert result["by_model"] == {generation.model: result["buckets"]}
    else:
        assert params["param_date"] == generation.created_at[:10]
        assert params["param_tag_key"] == "group"
        assert params["param_tag_value"] == "test"
        assert params["param_limit"] == ("3" if "events" in method else "5001")
        rows = result.data if method.endswith("_result") else result
        assert len(rows) == 1
        if "events" in method:
            assert rows[0]["id"] == generation.id
            assert rows[0]["cost_microdollars"] == 9
        else:
            assert rows[0]["requests"] == 1
            assert rows[0]["usage_microdollars"] == 9
        if method.endswith("_result"):
            assert not result.truncated
            assert result.scanned == 1


def test_postgres_activity_events_preserve_truncation(postgres_reader, generation):
    store, settings = postgres_reader("clickhouse")
    requests: list[httpx.Request] = []
    store._operational_analytics._transport = _transport(settings, generation, requests, copies=3)
    result = store.activity_events_result(generation.workspace_id, limit=2)
    assert result.truncated
    assert result.scanned == result.scan_limit == len(result.data) == 2
    assert requests[0].url.params["param_limit"] == "3"


@pytest.mark.parametrize("backend", ["postgres", "clickhouse"])
@pytest.mark.parametrize("group_by", [None, "none"])
def test_postgres_activity_route(postgres_reader, generation, backend, group_by):
    store, settings = postgres_reader(backend)
    requests: list[httpx.Request] = []
    if store._operational_analytics is not None:
        store._operational_analytics._transport = _transport(settings, generation, requests)
    workspace = Workspace(id=generation.workspace_id, name="test", owner_user_id="test-user")
    store._run_transaction(
        lambda conn: store._write_entity_tx(conn, "workspace", workspace.id, workspace)
    )
    configure_store(store)
    app = create_app(settings, configure_store_arg=False, init_observability=False)
    app.dependency_overrides[require_management] = lambda: Principal(
        user=None,
        workspace=store.get_workspace(workspace.id),
        api_key=None,
        is_management=True,
        scopes=frozenset(),
    )
    params = {"api_key_hash": generation.key_hash}
    if group_by is not None:
        params["group_by"] = group_by
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/v1/activity", params=params)
    if backend == "postgres":
        assert response.status_code == 500
        assert requests == []
    else:
        assert response.status_code == 200, response.text
        [row] = response.json()["data"]
        if group_by == "none":
            assert row["id"] == generation.id
        else:
            assert row["requests"] == 1
            assert row["usage_microdollars"] == 9
        assert response.json()["meta"]["scanned"] == 1
        assert len(requests) == 1


# Request tags for the console (tests/test_activity_tags.py covers the SQL).
_TAG_READS = [
    ("activity_tag_summary", {}),
    (
        "activity_tag_summary",
        {"group_by": "environment", "tag_key": "team", "tag_value": "search"},
    ),
    ("activity_tag_events", {"tag_key": "team", "tag_value": "search"}),
    ("activity_tag_events", {"tag_key": "environment", "limit": 3}),
]


@pytest.mark.parametrize(("method", "kwargs"), _TAG_READS)
def test_postgres_tag_reads_stay_unimplemented_by_default(
    postgres_reader: Any, method: str, kwargs: dict[str, Any]
) -> None:
    store, _ = postgres_reader()
    assert store._operational_analytics is None
    with pytest.raises(NotImplementedError) as error:
        getattr(store, method)(WORKSPACE, start_at=START_AT, **kwargs)
    assert str(error.value) == f"PostgresStore.{method} is not implemented in increment 1"


@pytest.mark.parametrize(("method", "kwargs"), _TAG_READS)
def test_postgres_tag_reads_match_gcp_and_memory(
    postgres_reader: Any, method: str, kwargs: dict[str, Any]
) -> None:
    """One implementation serves every plane, against that plane's ClickHouse."""
    store, settings = postgres_reader("clickhouse")
    requests: list[httpx.Request] = []
    recorded = recorded_handler(requests)
    tenant_id = analytics_surrogate("workspace", WORKSPACE)
    expected_auth = base64.b64encode(
        f"{settings.operational_analytics_clickhouse_user}:"
        f"{settings.operational_analytics_clickhouse_password}".encode()
    ).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "plane-clickhouse.test"
        assert request.url.params["database"] == "default"
        assert request.headers["authorization"] == f"Basic {expected_auth}"
        assert request.url.params["param_tenant_id"] == tenant_id
        sql = request.content.decode()
        assert "FROM activity_generations FINAL" in sql
        assert "tenant_id = {tenant_id:String}" in sql
        for text in (WORKSPACE, "team", "search", "environment"):
            assert text not in sql
        assert WORKSPACE not in str(request.url)
        return recorded(request)

    store._operational_analytics._transport = httpx.MockTransport(handler)
    spanner = SpannerStore.__new__(SpannerStore)
    spanner._operational_analytics = store._operational_analytics
    memory = InMemoryStore()
    for generation in fixture_generations():
        memory.add_generation(generation)

    result = getattr(store, method)(WORKSPACE, start_at=START_AT, **kwargs)
    sent = len(requests)

    assert result == getattr(spanner, method)(WORKSPACE, start_at=START_AT, **kwargs)
    assert result == getattr(memory, method)(WORKSPACE, start_at=START_AT, **kwargs)
    assert sent >= 1 and len(requests) == 2 * sent
    for first, second in zip(requests[:sent], requests[sent:], strict=True):
        assert first.url == second.url
        assert first.content == second.content
    if method == "activity_tag_events":
        [request] = requests[:sent]
        assert request.url.params["param_start_at"] == START_AT
        assert request.url.params["param_tag_key"] == kwargs["tag_key"]
        assert request.url.params["param_limit"] == str(kwargs.get("limit", 50) + 1)
        assert result.truncated is ("limit" in kwargs)
        assert [event["id"] for event in result.data] == (
            ["g10", "g05", "g03"] if "limit" in kwargs else ["g04", "g03", "g02", "g01"]
        )
    else:
        assert [row["key"] for row in result["keys"]] == ["team", "environment"]
        assert result["group_by"] == kwargs.get("group_by", "team")
        assert result["breakdown"]
