# ruff: noqa: F811 - imported pytest fixture
"""Request tags for the console on the Postgres store (AWS and Azure planes).

``PostgresStore`` and the Spanner store share one implementation of the tag
reads, each against its own plane's ClickHouse. ``tests/test_activity_tags.py``
covers the SQL; this file covers that the Postgres store reaches it only when
ClickHouse record reads are on, and then sends what the Spanner store sends.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest

from tests.activity_tags_recorded import (
    START_AT,
    WORKSPACE,
    fixture_generations,
    form_fields,
    read_name,
    recorded_handler,
    statement,
)

# The Postgres store under a fake connection pool, as the other record-read
# tests build it.
from tests.test_operational_analytics_reads_postgres import (
    postgres_reader,  # noqa: F401 - fixture
)
from trusted_router.storage import InMemoryStore
from trusted_router.storage_gcp import SpannerStore
from trusted_router.storage_operational_analytics import analytics_surrogate

_TAG_READS = [
    ("activity_tag_summary", {}),
    (
        "activity_tag_summary",
        {"group_by": "environment", "tag_key": "team", "tag_value": "search"},
    ),
    ("activity_tag_events", {"tag_key": "team", "tag_value": "search"}),
    ("activity_tag_events", {"tag_key": "team", "limit": 3}),
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
        # The plane's own database in the URL, and no bound value beside it.
        assert dict(request.url.params) == {"database": "default"}
        assert request.headers["authorization"] == f"Basic {expected_auth}"
        sql, bound = statement(request)
        assert bound["tenant_id"] == tenant_id
        assert "FROM activity_generations FINAL" in sql
        assert "tenant_id = {tenant_id:String}" in sql
        for text in (WORKSPACE, "team", "search", "environment"):
            assert text not in sql
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
        assert form_fields(first) == form_fields(second)
    reads = [read_name(statement(request)[0]) for request in requests[:sent]]
    if method == "activity_tag_events":
        assert reads == ["generations"]
        _sql, bound = statement(requests[0])
        assert bound["start_at"] == START_AT
        assert bound["tag_key"] == "team"
        assert bound["limit"] == str(kwargs.get("limit", 50) + 1)
        assert result.truncated is ("limit" in kwargs)
        assert [event["id"] for event in result.data] == (
            ["g16", "g15", "g14"] if "limit" in kwargs else ["g04", "g03", "g02", "g01"]
        )
    else:
        assert reads[0] == "keys" and reads[-3:] == ["totals", "histogram", "histogram"]
        assert [row["key"] for row in result["keys"]] == ["team", "environment"]
        assert result["group_by"] == kwargs.get("group_by", "team")
        assert result["breakdown"] and result["breakdown_too_many_values"] is False
