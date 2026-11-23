from __future__ import annotations

import base64
import contextlib
import dataclasses
import datetime as dt
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.fakes.clickhouse_activity import ActivityClickHouse
from tests.fakes.postgres import sqlite_postgres_conn
from trusted_router import storage_postgres
from trusted_router.auth import Principal, require_management
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.operational_analytics import usage_buckets_query
from trusted_router.storage import configure_store, create_store
from trusted_router.storage_activity import generation_events
from trusted_router.storage_gcp import SpannerStore
from trusted_router.storage_models import Generation, Workspace
from trusted_router.storage_operational_analytics import analytics_surrogate
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
    # The plane's ClickHouse: the shipped SQL runs on these rows.
    database = ActivityClickHouse()
    database.add(generation)
    for copy in range(1, copies):
        database.add(dataclasses.replace(generation, id=f"{generation.id}-{copy}"))

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
        return database.handle(request)

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
        # ClickHouse groups the range. No row limit, so nothing is cut off.
        assert "param_limit" not in params
        assert requests[0].content.decode() == usage_buckets_query(
            granularity="day", by_model=True, key_filtered=key_filtered,
        )
        assert result["truncated"] is False
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
        starts = [request.url.params.get("param_start_at") for request in requests]
        if group_by == "none":
            # Newest-first events with no date: two recent windows, then, the
            # account being this quiet, the whole history.
            assert len(starts) == 3
            assert starts[0] is not None and starts[1] is not None and starts[2] is None
            assert starts[1] < starts[0]
        else:
            assert starts == [None]


def _events_store(
    postgres_reader: Any, rows: list[Generation]
) -> tuple[PostgresStore, ActivityClickHouse]:
    store, _settings = postgres_reader("clickhouse")
    database = ActivityClickHouse()
    for row in rows:
        database.add(row)
    store._operational_analytics = database.client()
    return store, database


def _aged(generation: Generation, index: int, age: dt.timedelta) -> Generation:
    created = dt.datetime.now(dt.UTC) - age
    return dataclasses.replace(
        generation,
        id=f"gen-aged-{index}",
        created_at=created.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    )


def _unbounded_events(
    store: PostgresStore, generation: Generation, *, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    """What the read returned before it had windows: one query, no time bound."""
    rows = store._clickhouse_activity_rows(
        generation.workspace_id, api_key_hash=None, date=None, limit=limit + 1,
    )
    return generation_events(rows[:limit], limit=limit), len(rows) > limit


@pytest.mark.parametrize(
    ("ages", "limit", "queries"),
    [
        # A busy workspace fills the limit inside the last day: one bounded read.
        ([dt.timedelta(minutes=5 * n) for n in range(1, 8)], 5, 1),
        # Exactly `limit` rows, all recent. The read asks for one more to know
        # whether older rows exist ("truncated"), so it widens to find out.
        ([dt.timedelta(minutes=5 * n) for n in range(1, 6)], 5, 3),
        # The last day is short of the limit, the last 30 days are not.
        (
            [dt.timedelta(hours=2), *(dt.timedelta(days=3 + n) for n in range(6))],
            5,
            2,
        ),
        # Old rows only: the read ends on the whole history, as before.
        ([dt.timedelta(days=40 + n) for n in range(7)], 5, 3),
        # Fewer rows than asked for anywhere: every one of them comes back.
        ([dt.timedelta(hours=1), dt.timedelta(days=2), dt.timedelta(days=200)], 5, 3),
        # The "latest request" lookup of the usage panel.
        ([dt.timedelta(minutes=1), dt.timedelta(minutes=2), dt.timedelta(days=90)], 1, 1),
        ([dt.timedelta(days=90), dt.timedelta(days=91)], 1, 3),
        ([], 1, 3),
    ],
)
def test_newest_events_read_recent_windows_first_and_return_the_same_rows(
    postgres_reader: Any,
    generation: Generation,
    ages: list[dt.timedelta],
    limit: int,
    queries: int,
) -> None:
    rows = [_aged(generation, index, age) for index, age in enumerate(ages)]
    store, database = _events_store(postgres_reader, rows)

    result = store.activity_events_result(generation.workspace_id, limit=limit)

    assert len(database.queries) == queries
    starts = [params.get("start_at") for params in database.params]
    bounded = [start for start in starts if start is not None]
    # Every read but a final whole-history one is bounded, widest last.
    assert len(bounded) == min(queries, 2)
    assert starts == [*bounded, *[None] * (queries - len(bounded))]
    now = dt.datetime.now(dt.UTC)
    for start, days in zip(bounded, (1, 30), strict=False):
        age = now - dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
        assert dt.timedelta(days=days) <= age < dt.timedelta(days=days, minutes=1)
    assert all(params["limit"] == str(limit + 1) for params in database.params)
    # What the caller sees is what the single unbounded read returned.
    expected, truncated = _unbounded_events(store, generation, limit=limit)
    assert result.data == expected
    assert [event["id"] for event in result.data] == [row.id for row in rows[:limit]]
    assert result.truncated is truncated is (len(rows) > limit)
    assert result.scanned == min(len(rows), limit)


def test_dated_events_stay_one_read(postgres_reader: Any, generation: Generation) -> None:
    store, database = _events_store(postgres_reader, [generation])

    result = store.activity_events_result(
        generation.workspace_id, date=generation.created_at[:10], limit=5,
    )

    assert [event["id"] for event in result.data] == [generation.id]
    [params] = database.params
    assert "start_at" not in params
    assert params["date"] == generation.created_at[:10]


def test_newest_events_keep_key_and_tag_filters_in_every_window(
    postgres_reader: Any, generation: Generation
) -> None:
    other_key = dataclasses.replace(
        _aged(generation, 1, dt.timedelta(minutes=1)), key_hash="another-key-hash",
    )
    other_tag = dataclasses.replace(
        _aged(generation, 2, dt.timedelta(minutes=2)), tags={"group": "other"},
    )
    match = _aged(generation, 3, dt.timedelta(days=45))
    store, database = _events_store(postgres_reader, [other_key, other_tag, match])

    events = store.activity_events(
        generation.workspace_id,
        api_key_hash=generation.key_hash,
        tag_key="group",
        tag_value="test",
        limit=1,
    )

    assert [event["id"] for event in events] == [match.id]
    assert len(database.params) == 3
    for params in database.params:
        assert params["key_id"] == analytics_surrogate("api-key", generation.key_hash)
        assert params["tag_key"] == "group"
        assert params["tag_value"] == "test"
