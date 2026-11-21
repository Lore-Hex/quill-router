"""Synthetic tagged generations and what ClickHouse answered about them.

The durable stores answer the console's tag panel in ClickHouse and the
in-memory store answers it in Python. ``RECORDED`` is what ClickHouse 26.8
returned for the reader's exact SQL over ``FIXTURE``. The parity tests feed
those rows through the SQL path and require the in-memory store to agree, so
the two implementations are held to the database's own semantics (grouping,
ranking, the median, an empty value against an absent key) and not to a
second Python model of them.

To record again after changing a query::

    uv run python -m tests.activity_tags_recorded > recorded.sql
    clickhouse-client --multiquery < recorded.sql

The SQL is the reader's text with the table replaced by ``FIXTURE`` as a
literal subquery, so it needs no table and no grant. Each ``### name`` line in
the output is followed by that read's rows, in the same order as ``RECORDED``.
``tests/test_activity_tags.py`` pins the SQL text the rows were recorded for.

Every workspace, tag and number here is invented.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx

from trusted_router.operational_analytics import OperationalAnalyticsClient
from trusted_router.storage_models import Generation
from trusted_router.storage_operational_analytics import activity_payload, analytics_surrogate

WORKSPACE = "ws-tags-a"
OTHER_WORKSPACE = "ws-tags-b"
EMPTY_WORKSPACE = "ws-tags-none"
START_AT = "2026-03-01T00:00:00Z"

# id, workspace, created_at, tags, cost, usage type, prompt, completion, elapsed.
# g04 has no latency, g06 is BYOK, g08 carries an empty value, g09 and g10 lack
# the team key, g11 differs from "search" only by case, g12 is one millisecond
# before the window and g13 belongs to another workspace.
FIXTURE: list[tuple[str, str, str, dict[str, str], int, str, int, int, int | None]] = [
    ("g01", WORKSPACE, "2026-03-02T10:00:00.000Z", {"team": "search", "environment": "production"}, 900, "Credits", 100, 50, 400),  # noqa: E501
    ("g02", WORKSPACE, "2026-03-02T11:00:00.000Z", {"team": "search", "environment": "production"}, 300, "Credits", 40, 20, 200),  # noqa: E501
    ("g03", WORKSPACE, "2026-03-03T09:00:00.250Z", {"team": "search", "environment": "staging"}, 100, "Credits", 30, 10, 600),  # noqa: E501
    ("g04", WORKSPACE, "2026-03-03T09:30:00.000Z", {"team": "search"}, 50, "Credits", 20, 5, None),
    ("g05", WORKSPACE, "2026-03-04T08:00:00.000Z", {"team": "ads", "environment": "production"}, 700, "Credits", 70, 30, 150),  # noqa: E501
    ("g06", WORKSPACE, "2026-03-04T08:05:00.000Z", {"team": "ads"}, 500, "BYOK", 60, 25, 250),
    ("g07", WORKSPACE, "2026-03-04T12:00:00.000Z", {"team": "billing"}, 700, "Credits", 80, 40, 90),
    ("g08", WORKSPACE, "2026-03-05T01:00:00.000Z", {"team": ""}, 20, "Credits", 5, 5, 1000),
    ("g09", WORKSPACE, "2026-03-05T02:00:00.000Z", {}, 40, "Credits", 8, 4, 300),
    ("g10", WORKSPACE, "2026-03-05T03:00:00.000Z", {"environment": "production"}, 60, "Credits", 9, 3, 500),  # noqa: E501
    ("g11", WORKSPACE, "2026-03-05T04:00:00.000Z", {"team": "Search"}, 10, "Credits", 2, 1, 700),
    ("g12", WORKSPACE, "2026-02-28T23:59:59.999Z", {"team": "search", "environment": "production"}, 9999, "Credits", 999, 999, 5),  # noqa: E501
    ("g13", OTHER_WORKSPACE, "2026-03-02T10:30:00.000Z", {"team": "other-workspace", "environment": "production"}, 8888, "Credits", 888, 888, 7),  # noqa: E501
    ("g14", WORKSPACE, "2026-03-05T05:00:00.000Z", {"team": "zeta"}, 10, "Credits", 3, 2, 800),
]


def _breakdown(
    kind: str,
    value: str,
    values: int,
    requests: int,
    cost: int,
    byok: int,
    prompt: int,
    completion: int,
    median: int | None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "group_value": value,
        "value_count": values,
        "requests": requests,
        "cost_microdollars": cost,
        "byok_microdollars": byok,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "median_elapsed_ms": median,
    }


_TEAM_VALUES = [
    {"tag_value": "search", "requests": 4},
    {"tag_value": "ads", "requests": 2},
    {"tag_value": "", "requests": 1},
    {"tag_value": "Search", "requests": 1},
    {"tag_value": "billing", "requests": 1},
    {"tag_value": "zeta", "requests": 1},
]
_TEAM_ROWS = [
    _breakdown("value", "zeta", 1, 1, 10, 0, 3, 2, 800),
    _breakdown("value", "", 1, 1, 20, 0, 5, 5, 1000),
    _breakdown("value", "Search", 1, 1, 10, 0, 2, 1, 700),
    _breakdown("value", "billing", 1, 1, 700, 0, 80, 40, 90),
    _breakdown("value", "ads", 1, 2, 700, 500, 130, 55, 150),
    _breakdown("value", "search", 1, 4, 1350, 0, 190, 85, 400),
]
_TEAM_UNTAGGED = _breakdown("untagged", "", 0, 2, 100, 0, 17, 7, 300)

# Keyed by the read and its bound values:
#   ("keys", workspace, limit)
#   ("values", workspace, tag_key, limit)
#   ("breakdown", workspace, group_by, limit, tag_key, tag_value)
# Keys and values are in the order ClickHouse returned them. A breakdown has no
# ORDER BY, so the order of its rows carries no meaning.
RECORDED: dict[tuple[Any, ...], list[dict[str, Any]]] = {
    ("keys", WORKSPACE, 101): [
        {"tag_key": "team", "requests": 10},
        {"tag_key": "environment", "requests": 5},
    ],
    ("keys", WORKSPACE, 2): [
        {"tag_key": "team", "requests": 10},
        {"tag_key": "environment", "requests": 5},
    ],
    ("keys", OTHER_WORKSPACE, 101): [
        {"tag_key": "environment", "requests": 1},
        {"tag_key": "team", "requests": 1},
    ],
    ("keys", EMPTY_WORKSPACE, 101): [],
    ("values", WORKSPACE, "team", 51): _TEAM_VALUES,
    ("values", WORKSPACE, "team", 3): _TEAM_VALUES[:3],
    ("values", WORKSPACE, "environment", 51): [
        {"tag_value": "production", "requests": 4},
        {"tag_value": "staging", "requests": 1},
    ],
    ("breakdown", WORKSPACE, "team", 50, None, None): [
        _TEAM_ROWS[0],
        _TEAM_UNTAGGED,
        *_TEAM_ROWS[1:],
    ],
    ("breakdown", WORKSPACE, "team", 2, None, None): [
        _breakdown("other", "", 4, 4, 740, 0, 90, 48, 700),
        _TEAM_UNTAGGED,
        _TEAM_ROWS[4],
        _TEAM_ROWS[5],
    ],
    ("breakdown", WORKSPACE, "environment", 50, "team", "search"): [
        _breakdown("value", "staging", 1, 1, 100, 0, 30, 10, 600),
        _breakdown("value", "production", 1, 2, 1200, 0, 140, 70, 200),
        _breakdown("untagged", "", 0, 1, 50, 0, 20, 5, None),
    ],
    ("breakdown", WORKSPACE, "team", 50, "team", None): _TEAM_ROWS,
    ("breakdown", WORKSPACE, "environment", 2, None, None): [
        _breakdown("value", "staging", 1, 1, 100, 0, 30, 10, 600),
        _breakdown("value", "production", 1, 4, 1960, 0, 219, 103, 200),
        _breakdown("untagged", "", 0, 7, 830, 500, 178, 82, 300),
    ],
    ("breakdown", WORKSPACE, "environment", 2, "team", None): [
        _breakdown("value", "staging", 1, 1, 100, 0, 30, 10, 600),
        _breakdown("value", "production", 1, 3, 1900, 0, 210, 100, 200),
        _breakdown("untagged", "", 0, 6, 790, 500, 170, 78, 700),
    ],
    ("breakdown", WORKSPACE, "team", 50, "team", ""): [_TEAM_ROWS[1]],
    ("breakdown", WORKSPACE, "cost-center", 50, None, None): [
        _breakdown("untagged", "", 0, 12, 2890, 500, 427, 195, 400),
    ],
    ("breakdown", OTHER_WORKSPACE, "environment", 50, None, None): [
        _breakdown("value", "production", 1, 1, 8888, 0, 888, 888, 7),
    ],
}


def fixture_generations() -> list[Generation]:
    return [
        Generation(
            id=generation_id,
            request_id=f"req-{generation_id}",
            workspace_id=workspace,
            key_hash="key-hash-tags",
            model="test/model",
            provider="test-provider",
            provider_name="Test Provider",
            app="tags-test",
            tokens_prompt=prompt,
            tokens_completion=completion,
            total_cost_microdollars=cost,
            usage_type=usage_type,
            speed_tokens_per_second=10.0,
            finish_reason="stop",
            status="success",
            streamed=False,
            elapsed_milliseconds=elapsed,
            tags=dict(tags),
            created_at=created_at,
        )
        for (
            generation_id, workspace, created_at, tags, cost, usage_type, prompt, completion,
            elapsed,
        ) in FIXTURE
    ]


_WORKSPACE_BY_TENANT = {
    analytics_surrogate("workspace", workspace): workspace
    for workspace in (WORKSPACE, OTHER_WORKSPACE, EMPTY_WORKSPACE)
}


def bound_values(request: httpx.Request) -> dict[str, str]:
    return {
        name.removeprefix("param_"): value
        for name, value in request.url.params.items()
        if name.startswith("param_")
    }


def recorded_key(sql: str, params: dict[str, str]) -> tuple[Any, ...]:
    workspace = _WORKSPACE_BY_TENANT[params["tenant_id"]]
    if "arrayJoin(mapKeys(tags))" in sql:
        return ("keys", workspace, int(params["limit"]))
    if "AS top_value" in sql:
        return (
            "breakdown", workspace, params["group_by"], int(params["limit"]),
            params.get("tag_key"), params.get("tag_value"),
        )
    return ("values", workspace, params["tag_key"], int(params["limit"]))


def recorded_handler(
    requests: list[httpx.Request],
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer the tag reads with ``RECORDED``; an unrecorded read is an error."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        sql = request.content.decode()
        params = bound_values(request)
        assert params["start_at"] == START_AT
        if "generation_id" in sql:
            return httpx.Response(200, json={"data": _generation_rows(params)})
        return httpx.Response(200, json={"data": RECORDED[recorded_key(sql, params)]})

    return handler


def recorded_transport(requests: list[httpx.Request]) -> httpx.MockTransport:
    return httpx.MockTransport(recorded_handler(requests))


def recorded_client(requests: list[httpx.Request]) -> OperationalAnalyticsClient:
    return OperationalAnalyticsClient(
        base_url="http://clickhouse.test:8123",
        user="tr_control_read",
        password="test-reader",  # noqa: S106 - inert test credential.
        transport=recorded_transport(requests),
    )


def _generation_rows(params: dict[str, str]) -> list[dict[str, Any]]:
    """The row reader that predates the tag panel, over ``FIXTURE``.

    ``activity_generations`` is not recorded: the tagged request list reuses
    it unchanged, and ``tests/test_operational_analytics.py`` covers its SQL.
    """
    rows = [
        generation
        for generation in fixture_generations()
        if analytics_surrogate("workspace", generation.workspace_id) == params["tenant_id"]
        and generation.created_at >= params["start_at"]
        and ("tag_key" not in params or params["tag_key"] in generation.tags)
        and (
            "tag_value" not in params
            or generation.tags.get(params["tag_key"]) == params["tag_value"]
        )
    ]
    rows.sort(key=lambda generation: generation.created_at, reverse=True)
    payloads = []
    for generation in rows[: int(params["limit"])]:
        payload = activity_payload(generation)
        payload.pop("workspace_id")
        # ClickHouse prints DateTime64 without the "T" and the zone.
        payload["created_at"] = generation.created_at.replace("T", " ").removesuffix("Z")
        payloads.append(payload)
    return payloads


def _literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _fixture_subquery() -> str:
    rows = []
    for (
        _id, workspace, created_at, tags, cost, usage_type, prompt, completion, elapsed,
    ) in FIXTURE:
        pairs = ", ".join(f"{_literal(key)}, {_literal(value)}" for key, value in tags.items())
        latency = "NULL" if elapsed is None else str(elapsed)
        moment = created_at.replace("T", " ").removesuffix("Z")
        rows.append(
            f"  ({_literal(analytics_surrogate('workspace', workspace))}, "
            f"toDateTime64({_literal(moment)}, 3, 'UTC'), "
            f"CAST(map({pairs}), 'Map(String, String)'), toInt64({cost}), "
            f"{_literal(usage_type)}, toUInt64({prompt}), toUInt64({completion}), "
            f"CAST({latency}, 'Nullable(UInt64)'))"
        )
    # Literals of the invented FIXTURE above, for the recording script only.
    return (
        "(SELECT r.1 AS tenant_id, r.2 AS created_at, r.3 AS tags, "  # noqa: S608
        "r.4 AS total_cost_microdollars, r.5 AS usage_type, r.6 AS tokens_prompt, "
        "r.7 AS tokens_completion, r.8 AS elapsed_milliseconds FROM (SELECT arrayJoin([\n"
        + ",\n".join(rows)
        + "\n]) AS r)) AS activity_generations"
    )


def reader_sql(key: tuple[Any, ...]) -> tuple[str, dict[str, str]]:
    """The SQL and bound values the reader sends for one ``RECORDED`` key."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"data": []})

    client = OperationalAnalyticsClient(
        base_url="http://clickhouse.test:8123",
        user="tr_control_read",
        password="test-reader",  # noqa: S106 - inert test credential.
        transport=httpx.MockTransport(handler),
    )
    tenant_id = analytics_surrogate("workspace", key[1])
    if key[0] == "keys":
        client.activity_tag_keys(tenant_id=tenant_id, start_at=START_AT, limit=key[2])
    elif key[0] == "values":
        client.activity_tag_values(
            tenant_id=tenant_id, tag_key=key[2], start_at=START_AT, limit=key[3]
        )
    else:
        client.activity_tag_breakdown(
            tenant_id=tenant_id,
            group_by=key[2],
            start_at=START_AT,
            limit=key[3],
            tag_key=key[4],
            tag_value=key[5],
        )
    [request] = sent
    return request.content.decode(), bound_values(request)


def recording_sql() -> str:
    """One multiquery script that records every ``RECORDED`` key."""
    table = _fixture_subquery()
    statements = []
    for key in RECORDED:
        sql, params = reader_sql(key)
        assert "FROM activity_generations FINAL" in sql
        sql = sql.replace("FROM activity_generations FINAL", "FROM " + table)
        sql = re.sub(r"FORMAT JSON\s*$", "FORMAT JSONEachRow", sql.strip())
        label = " ".join("-" if part is None else str(part) for part in key)
        statements.append(f"SELECT {_literal('### ' + label)};")
        statements.extend(
            f"SET param_{name} = {_literal(value)};" for name, value in params.items()
        )
        statements.append(sql + ";")
    return "\n".join(statements) + "\n"


if __name__ == "__main__":
    print(recording_sql(), end="")
