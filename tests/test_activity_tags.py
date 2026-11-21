"""Request tags for the console: the reads, their SQL, parity and privacy.

Tag keys and values in this file are invented.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import httpx
import pytest
import sentry_sdk
from sentry_sdk.integrations.httpx import HttpxIntegration
from sentry_sdk.transport import Transport

from tests.activity_tags_recorded import (
    EMPTY_WORKSPACE,
    OTHER_WORKSPACE,
    RECORDED,
    START_AT,
    WORKSPACE,
    bound_values,
    fixture_generations,
    reader_sql,
    recorded_client,
    recorded_key,
)
from trusted_router import storage_activity_tags
from trusted_router.operational_analytics import (
    OperationalAnalyticsClient,
    bound_values_stay_private,
    is_analytics_unavailable,
    log_analytics_unavailable,
)
from trusted_router.storage import InMemoryStore
from trusted_router.storage_activity_tags import (
    break_down_by_tag,
    count_tag_keys,
    count_tag_values,
    generations_in_window,
    lower_median,
    order_breakdown,
    parse_window_start,
    tag_summary,
)
from trusted_router.storage_gcp import SpannerStore
from trusted_router.storage_operational_analytics import analytics_surrogate

# The SQL text RECORDED was captured for, by shape: (read, has a tag filter,
# the filter names a value). A query that changes must be recorded again
# (see tests/activity_tags_recorded.py) before its fingerprint is updated.
SQL_FINGERPRINTS = {
    ("keys", False, False): "3ab56b7986d2898b75d02a823f9acf58a57f4df124538e4608693afd37ddbde8",
    ("values", False, False): "02a5cfde90cb35e84a00808e54bd830074abfa12aaa8701cac52a6a16ec0a861",
    ("breakdown", False, False): "26c9bbd2b80eee38ed6930abf0d4c679cf206d8d2d89b0d30359290df317d7cb",
    ("breakdown", True, False): "13411dee0f5045c9676e18ca16d973912a65e9bc90d027ea3ccfe4e7cf3438df",
    ("breakdown", True, True): "6be2aaf080564d302d0daea7b8a9a40c37b9e0db40ccb36722b00a00b9cd2ed5",
}


def _memory_store() -> InMemoryStore:
    store = InMemoryStore()
    for generation in fixture_generations():
        store.add_generation(generation)
    return store


def _sql_store(requests: list[httpx.Request]) -> SpannerStore:
    store = SpannerStore.__new__(SpannerStore)
    store._operational_analytics = recorded_client(requests)
    return store


def _client(handler: Any) -> OperationalAnalyticsClient:
    return OperationalAnalyticsClient(
        base_url="http://clickhouse.test:8123",
        user="tr_control_read",
        password="test-reader",  # noqa: S106 - inert test credential.
        transport=httpx.MockTransport(handler),
    )


# ------------------------------------------------------------ pure functions


def test_lower_median_matches_the_clickhouse_function() -> None:
    # quantileExactWeighted(0.5)(x, 1), measured on ClickHouse 26.8: the lower
    # of the two middle values for an even count, never their average.
    assert lower_median([]) is None
    assert lower_median([30]) == 30
    assert lower_median([10, 21]) == 10
    assert lower_median([30, 10, 20]) == 20
    assert lower_median([40, 10, 30, 20]) == 20


def test_window_start_reads_zulu_offset_and_naive_times_as_utc() -> None:
    assert parse_window_start("2026-03-01T00:00:00Z") == parse_window_start(
        "2026-03-01T00:00:00+00:00"
    )
    assert parse_window_start("2026-03-01T00:00:00").utcoffset() is not None
    # A fraction sorts after the whole second it extends, which a plain string
    # comparison gets wrong ("…00.250Z" < "…00Z").
    assert parse_window_start("2026-03-01T00:00:00.250Z") > parse_window_start(START_AT)


def test_window_keeps_one_workspace_from_the_start_and_matches_tags_exactly() -> None:
    generations = fixture_generations()

    window = generations_in_window(generations, workspace_id=WORKSPACE, start_at=START_AT)
    assert [gen.id for gen in window] == [
        "g01", "g02", "g03", "g04", "g05", "g06", "g07", "g08", "g09", "g10", "g11", "g14",
        "g15", "g16",
    ]

    def matching(**tag: str | None) -> list[str]:
        return [
            gen.id
            for gen in generations_in_window(
                generations, workspace_id=WORKSPACE, start_at=START_AT, **tag
            )
        ]

    assert matching(tag_key="environment") == ["g01", "g02", "g03", "g05", "g10"]
    assert matching(tag_key="team", tag_value="search") == ["g01", "g02", "g03", "g04"]
    # Case-sensitive, and an empty value is a value, not a missing key.
    assert matching(tag_key="team", tag_value="Search") == ["g11"]
    assert matching(tag_key="team", tag_value="") == ["g08"]
    assert matching(tag_key="cost-center") == []


def test_keys_and_values_rank_by_requests_then_text() -> None:
    window = generations_in_window(
        fixture_generations(), workspace_id=WORKSPACE, start_at=START_AT
    )

    assert count_tag_keys(window, limit=10) == [
        {"key": "team", "requests": 12},
        {"key": "environment", "requests": 5},
    ]
    assert count_tag_keys(window, limit=1) == [{"key": "team", "requests": 12}]
    # Equal counts fall back to the text: "ads" before "zeta", "" before "Search".
    assert count_tag_values(window, tag_key="team", limit=5) == [
        {"value": "search", "requests": 4},
        {"value": "ads", "requests": 2},
        {"value": "zeta", "requests": 2},
        {"value": "", "requests": 1},
        {"value": "Search", "requests": 1},
    ]


def test_breakdown_folds_the_tail_into_other_and_keeps_untagged_apart() -> None:
    window = generations_in_window(
        fixture_generations(), workspace_id=WORKSPACE, start_at=START_AT
    )

    rows = order_breakdown(break_down_by_tag(window, group_by="team", limit=2))

    assert [(row["kind"], row["value"], row["values"]) for row in rows] == [
        ("value", "search", 1),
        # The cut is by cost: "billing" (800, one request) keeps its row and
        # "ads" (700 in credits, two requests) does not.
        ("value", "billing", 1),
        ("other", None, 5),
        ("untagged", None, 0),
    ]
    search, billing, other, untagged = rows
    assert search["cost_microdollars"] == 1350
    assert search["median_elapsed_ms"] == 400  # g04 has no latency and is not counted
    assert (billing["requests"], billing["cost_microdollars"]) == (1, 800)
    # BYOK spend is reported apart from credit cost, as the usage chart does.
    assert (other["requests"], other["cost_microdollars"], other["byok_microdollars"]) == (
        7, 750, 500,
    )
    assert other["median_elapsed_ms"] == 800
    assert (untagged["requests"], untagged["cost_microdollars"]) == (2, 100)
    # Nothing is lost: the rows add up to the window.
    assert sum(row["requests"] for row in rows) == len(window)
    assert sum(row["prompt_tokens"] for row in rows) == sum(gen.tokens_prompt for gen in window)


@pytest.mark.parametrize(
    ("limit", "kept", "folded"),
    [
        # Cost decides before requests: billing over ads.
        (2, ["search", "billing"], 5),
        # At equal cost, more requests first: zeta over Search and delta.
        (5, ["search", "billing", "ads", "", "zeta"], 2),
        # At equal cost and requests, the text decides: Search over delta.
        (6, ["search", "billing", "ads", "", "zeta", "Search"], 1),
        (7, ["search", "billing", "ads", "", "zeta", "Search", "delta"], 0),
    ],
)
def test_the_values_that_keep_a_row_are_the_most_expensive(
    limit: int, kept: list[str], folded: int
) -> None:
    window = generations_in_window(
        fixture_generations(), workspace_id=WORKSPACE, start_at=START_AT
    )

    rows = order_breakdown(break_down_by_tag(window, group_by="team", limit=limit))

    assert [row["value"] for row in rows if row["kind"] == "value"] == kept
    assert [row["values"] for row in rows if row["kind"] == "other"] == ([folded] if folded else [])


def test_summary_spends_one_read_on_a_workspace_without_tags() -> None:
    calls: list[str] = []

    def read_keys(limit: int) -> list[dict[str, Any]]:
        calls.append(f"keys:{limit}")
        return []

    def never(*_args: Any) -> list[dict[str, Any]]:
        raise AssertionError("no further read is needed when there are no tags")

    summary = tag_summary(
        read_keys=read_keys,
        read_values=never,
        read_breakdown=never,
        group_by="team",
        tag_key="team",
    )

    assert calls == ["keys:101"]
    assert summary == {
        "keys": [],
        "keys_truncated": False,
        "group_by": None,
        "breakdown": [],
        "filter_values": [],
        "filter_values_truncated": False,
    }


# ------------------------------------------------------------- query builders


def test_recorded_answers_belong_to_the_sql_the_reader_sends_today() -> None:
    seen: dict[tuple[str, bool, bool], str] = {}
    for key in RECORDED:
        sql, params = reader_sql(key)
        shape = (key[0], key[0] == "breakdown" and "tag_key" in params, "tag_value" in params)
        digest = hashlib.sha256(sql.encode()).hexdigest()
        assert seen.setdefault(shape, digest) == digest, "one shape, one SQL text"
    assert seen == SQL_FINGERPRINTS, (
        "a tag query changed: record its ClickHouse answers again "
        "(tests/activity_tags_recorded.py), then update SQL_FINGERPRINTS"
    )


@pytest.mark.parametrize("key", list(RECORDED), ids=lambda key: "-".join(map(str, key)))
def test_tag_reads_bind_every_value_and_cover_the_whole_window(key: tuple[Any, ...]) -> None:
    sql, params = reader_sql(key)

    # Deduplicated rows of one tenant since the window start: no sample and no
    # row limit below the aggregate.
    assert sql.count("FROM activity_generations FINAL") == (2 if key[0] == "breakdown" else 1)
    assert sql.count("tenant_id = {tenant_id:String}") == sql.count("FROM activity_generations")
    assert sql.count("created_at >= parseDateTime64BestEffort({start_at:String}, 3)") == sql.count(
        "FROM activity_generations"
    )
    assert params["tenant_id"] == analytics_surrogate("workspace", key[1])
    assert params["start_at"] == START_AT
    # The only LIMIT bounds the number of groups, and it is bound too.
    assert sql.count("LIMIT") == 1
    assert "LIMIT {limit:UInt32}" in sql
    # No workspace id, tag key or tag value is ever part of the SQL text.
    for text in (key[1], "team", "environment", "search", "cost-center"):
        assert text not in sql
    assert recorded_key(sql, params) == key
    if key[0] == "breakdown":
        assert params["group_by"] == key[2]
        assert ("tag_key" in params, "tag_value" in params) == (
            key[4] is not None, key[5] is not None,
        )
        # A tag filter narrows the ranking pass and the totals pass alike.
        assert sql.count("mapContains(tags, {tag_key:String})") == (2 if key[4] is not None else 0)
        assert sql.count("tags[{tag_key:String}] = {tag_value:String}") == (
            2 if key[5] is not None else 0
        )
        assert "quantileExactWeighted(0.5)(elapsed_milliseconds, 1)" in sql


def test_reader_limits_are_at_least_one_and_rows_are_typed() -> None:
    limits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        limits.append(request.url.params["param_limit"])
        sql = request.content.decode()
        if "AS top_value" in sql:
            # A reader profile that quotes 64-bit integers returns strings.
            row = {
                "kind": "other", "group_value": "", "value_count": "3", "requests": "7",
                "cost_microdollars": "11", "byok_microdollars": "0", "prompt_tokens": "5",
                "completion_tokens": "2", "median_elapsed_ms": None,
            }
            return httpx.Response(200, json={"data": [row]})
        name = "tag_key" if "arrayJoin" in sql else "tag_value"
        return httpx.Response(200, json={"data": [{name: "team", "requests": "9"}]})

    client = _client(handler)

    assert client.activity_tag_keys(tenant_id="t", start_at=START_AT, limit=0) == [
        {"key": "team", "requests": 9}
    ]
    assert client.activity_tag_values(
        tenant_id="t", tag_key="team", start_at=START_AT, limit=-5
    ) == [{"value": "team", "requests": 9}]
    assert client.activity_tag_breakdown(
        tenant_id="t", group_by="team", start_at=START_AT, limit=0
    ) == [
        {
            "kind": "other", "value": None, "values": 3, "requests": 7, "cost_microdollars": 11,
            "byok_microdollars": 0, "prompt_tokens": 5, "completion_tokens": 2,
            "median_elapsed_ms": None,
        }
    ]
    assert limits == ["1", "1", "1"]


def test_reader_refuses_a_breakdown_row_it_does_not_know() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"kind": "sample", "group_value": ""}]})

    with pytest.raises(RuntimeError, match="unknown row kind"):
        _client(handler).activity_tag_breakdown(
            tenant_id="t", group_by="team", start_at=START_AT, limit=50
        )


# ---------------------------------------------------- the fake analytics client


class _FakeAnalytics:
    """Stands in for the ClickHouse reader, like the other console tests' fakes."""

    def __init__(self, *, keys: list[dict[str, Any]]) -> None:
        self.keys = keys
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def activity_tag_keys(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("keys", kwargs))
        return self.keys[: kwargs["limit"]]

    def activity_tag_values(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("values", kwargs))
        return [{"value": f"v{index}", "requests": 1} for index in range(kwargs["limit"])]

    def activity_tag_breakdown(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("breakdown", kwargs))
        return []

    def activity_generations(self, **kwargs: Any) -> list[Any]:
        self.calls.append(("generations", kwargs))
        return fixture_generations()[: kwargs["limit"]]


def _store_with(fake: _FakeAnalytics) -> SpannerStore:
    store = SpannerStore.__new__(SpannerStore)
    store._operational_analytics = fake
    return store


def test_store_reads_one_tenant_and_asks_for_one_row_past_each_limit() -> None:
    fake = _FakeAnalytics(keys=[{"key": f"key-{index:03d}", "requests": 1} for index in range(150)])
    tenant_id = analytics_surrogate("workspace", WORKSPACE)

    summary = _store_with(fake).activity_tag_summary(
        WORKSPACE, start_at=START_AT, group_by="key-140", tag_key="team", tag_value="search"
    )

    assert fake.calls == [
        ("keys", {"tenant_id": tenant_id, "start_at": START_AT, "limit": 101}),
        ("values", {"tenant_id": tenant_id, "tag_key": "team", "start_at": START_AT, "limit": 51}),
        (
            "breakdown",
            {
                "tenant_id": tenant_id, "group_by": "key-140", "start_at": START_AT, "limit": 50,
                "tag_key": "team", "tag_value": "search",
            },
        ),
    ]
    assert len(summary["keys"]) == 100 and summary["keys_truncated"] is True
    assert len(summary["filter_values"]) == 50 and summary["filter_values_truncated"] is True
    # The requested key is past the 100 listed keys. The list was cut, so the
    # key may well exist and is kept instead of being replaced by the default.
    assert summary["group_by"] == "key-140"
    for _name, kwargs in fake.calls:
        assert WORKSPACE not in kwargs.values()


def test_store_falls_back_to_the_most_used_key_when_the_asked_one_is_absent() -> None:
    fake = _FakeAnalytics(keys=[{"key": "team", "requests": 3}, {"key": "tier", "requests": 1}])

    summary = _store_with(fake).activity_tag_summary(
        WORKSPACE, start_at=START_AT, group_by="cost-center"
    )

    assert summary["group_by"] == "team"
    assert [name for name, _kwargs in fake.calls] == ["keys", "breakdown"]
    assert fake.calls[1][1]["group_by"] == "team"


def test_store_tagged_requests_are_bounded_in_time_and_number() -> None:
    fake = _FakeAnalytics(keys=[])
    tenant_id = analytics_surrogate("workspace", WORKSPACE)

    found = _store_with(fake).activity_tag_events(
        WORKSPACE, start_at=START_AT, tag_key="team", tag_value="search", limit=5000
    )

    # Never more than 50 rows, whatever the caller asks for, and always from a
    # window start: the unbounded list read this replaces scanned all history.
    assert fake.calls == [
        (
            "generations",
            {
                "tenant_id": tenant_id, "tag_key": "team", "tag_value": "search",
                "start_at": START_AT, "limit": 51,
            },
        )
    ]
    assert found.scan_limit == 50


def test_store_without_a_reader_says_so() -> None:
    store = SpannerStore.__new__(SpannerStore)
    store._operational_analytics = None

    with pytest.raises(RuntimeError, match="not configured"):
        store.activity_tag_summary(WORKSPACE, start_at=START_AT)
    with pytest.raises(RuntimeError, match="not configured"):
        store.activity_tag_events(WORKSPACE, start_at=START_AT, tag_key="team")


# ------------------------------------------------- in-memory vs ClickHouse parity


@pytest.mark.parametrize(
    ("workspace", "kwargs", "reads"),
    [
        (WORKSPACE, {}, 2),
        (WORKSPACE, {"group_by": "environment", "tag_key": "team", "tag_value": "search"}, 3),
        (WORKSPACE, {"group_by": "team", "tag_key": "team"}, 3),
        (WORKSPACE, {"group_by": "team", "tag_key": "team", "tag_value": ""}, 3),
        # A key no request carries is replaced by the most used key.
        (WORKSPACE, {"group_by": "cost-center"}, 2),
        (OTHER_WORKSPACE, {}, 2),
        (EMPTY_WORKSPACE, {"group_by": "team", "tag_key": "team", "tag_value": "search"}, 1),
    ],
    ids=["default", "filter", "key-only-filter", "empty-value", "absent-key", "other-workspace",
         "no-tags"],
)
def test_memory_and_clickhouse_agree_on_the_tag_summary(
    workspace: str, kwargs: dict[str, Any], reads: int
) -> None:
    requests: list[httpx.Request] = []

    from_sql = _sql_store(requests).activity_tag_summary(workspace, start_at=START_AT, **kwargs)
    from_memory = _memory_store().activity_tag_summary(workspace, start_at=START_AT, **kwargs)

    assert from_memory == from_sql
    assert len(requests) == reads
    for request in requests:
        assert bound_values(request)["tenant_id"] == analytics_surrogate("workspace", workspace)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"group_by": "team"},
        # "environment" is outside the cut key list and is kept.
        {"group_by": "environment"},
        {"group_by": "environment", "tag_key": "team"},
    ],
    ids=["other-row", "key-outside-the-list", "values-cut"],
)
def test_memory_and_clickhouse_agree_when_keys_and_values_are_cut(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
) -> None:
    monkeypatch.setattr(storage_activity_tags, "TAG_KEY_LIMIT", 1)
    monkeypatch.setattr(storage_activity_tags, "TAG_VALUE_LIMIT", 2)

    from_sql = _sql_store([]).activity_tag_summary(WORKSPACE, start_at=START_AT, **kwargs)
    from_memory = _memory_store().activity_tag_summary(WORKSPACE, start_at=START_AT, **kwargs)

    assert from_memory == from_sql
    assert from_sql["keys"] == [{"key": "team", "requests": 12}]
    assert from_sql["keys_truncated"] is True
    assert from_sql["group_by"] == kwargs["group_by"]
    assert from_sql["filter_values_truncated"] is ("tag_key" in kwargs)


@pytest.mark.parametrize(
    ("limit", "kept", "folded"),
    [
        (2, ["search", "billing"], 5),
        (5, ["search", "billing", "ads", "", "zeta"], 2),
        (6, ["search", "billing", "ads", "", "zeta", "Search"], 1),
    ],
)
def test_memory_and_clickhouse_keep_the_same_values_at_every_cut(
    monkeypatch: pytest.MonkeyPatch, limit: int, kept: list[str], folded: int
) -> None:
    """Which values survive the cap is decided by cost, then requests, then text.

    Each limit puts the cut where a different one of those three decides, so
    a ranking that drops or reorders one of them, in SQL or in Python, shows.
    """
    monkeypatch.setattr(storage_activity_tags, "TAG_VALUE_LIMIT", limit)

    from_sql = _sql_store([]).activity_tag_summary(WORKSPACE, start_at=START_AT, group_by="team")
    from_memory = _memory_store().activity_tag_summary(
        WORKSPACE, start_at=START_AT, group_by="team"
    )

    assert from_memory == from_sql
    rows = from_sql["breakdown"]
    assert [row["value"] for row in rows if row["kind"] == "value"] == kept
    assert [row["values"] for row in rows if row["kind"] == "other"] == [folded]
    assert [row["kind"] for row in rows][-2:] == ["other", "untagged"]


def test_the_agreed_summary_is_the_expected_one() -> None:
    summary = _memory_store().activity_tag_summary(
        WORKSPACE, start_at=START_AT, group_by="environment", tag_key="team", tag_value="search"
    )

    def row(kind: str, value: str | None, values: int, *numbers: Any) -> dict[str, Any]:
        names = (
            "requests", "cost_microdollars", "byok_microdollars", "prompt_tokens",
            "completion_tokens", "median_elapsed_ms",
        )
        return {"kind": kind, "value": value, "values": values, **dict(zip(names, numbers, strict=True))}

    assert summary == {
        "keys": [{"key": "team", "requests": 12}, {"key": "environment", "requests": 5}],
        "keys_truncated": False,
        "group_by": "environment",
        "breakdown": [
            row("value", "production", 1, 2, 1200, 0, 140, 70, 200),
            row("value", "staging", 1, 1, 100, 0, 30, 10, 600),
            row("untagged", None, 0, 1, 50, 0, 20, 5, None),
        ],
        # The value list describes the whole window, not the filtered rows.
        "filter_values": [
            {"value": "search", "requests": 4},
            {"value": "ads", "requests": 2},
            {"value": "zeta", "requests": 2},
            {"value": "", "requests": 1},
            {"value": "Search", "requests": 1},
            {"value": "billing", "requests": 1},
            {"value": "delta", "requests": 1},
        ],
        "filter_values_truncated": False,
    }


@pytest.mark.parametrize(
    ("kwargs", "ids", "truncated"),
    [
        ({"tag_key": "team", "tag_value": "search"}, ["g04", "g03", "g02", "g01"], False),
        ({"tag_key": "environment"}, ["g10", "g05", "g03", "g02", "g01"], False),
        ({"tag_key": "team", "tag_value": ""}, ["g08"], False),
        ({"tag_key": "team", "limit": 3}, ["g16", "g15", "g14"], True),
        ({"tag_key": "cost-center"}, [], False),
    ],
    ids=["key-and-value", "key-only", "empty-value", "cut", "no-match"],
)
def test_memory_and_clickhouse_agree_on_tagged_requests(
    kwargs: dict[str, Any], ids: list[str], truncated: bool
) -> None:
    from_sql = _sql_store([]).activity_tag_events(WORKSPACE, start_at=START_AT, **kwargs)
    from_memory = _memory_store().activity_tag_events(WORKSPACE, start_at=START_AT, **kwargs)

    assert from_memory == from_sql
    assert [event["id"] for event in from_sql.data] == ids
    assert from_sql.truncated is truncated


# -------------------------------------------------------------------- privacy


class _CapturedEnvelopes(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[str] = []

    def capture_envelope(self, envelope: Any) -> None:
        for item in envelope.items:
            self.payloads.append(str(item.payload.json))


def test_tag_reads_leave_no_bound_value_in_sentry() -> None:
    """Sentry's httpx integration records an outgoing request's query string.

    ClickHouse receives bound values in that query string, so a tag read must
    run where the integration is off. This uses the real SDK: a transaction
    with a tag read in it, then an error event, and nothing sent carries the
    tag. The plain row reader inside the same transaction is the positive
    control that the capture would have seen it.
    """
    client = recorded_client([])
    transport = _CapturedEnvelopes()
    sentry = sentry_sdk.Client(
        dsn="https://public@sentry.invalid/1",
        transport=transport,
        default_integrations=False,
        auto_enabling_integrations=False,
        integrations=[HttpxIntegration()],
        traces_sample_rate=1.0,
    )
    tenant_id = analytics_surrogate("workspace", WORKSPACE)
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(sentry)
        with sentry_sdk.start_transaction(name="tag-reads"):
            client.activity_tag_keys(tenant_id=tenant_id, start_at=START_AT, limit=101)
            client.activity_tag_values(
                tenant_id=tenant_id, tag_key="team", start_at=START_AT, limit=51
            )
            client.activity_tag_breakdown(
                tenant_id=tenant_id, group_by="environment", start_at=START_AT, limit=50,
                tag_key="team", tag_value="search",
            )
            with bound_values_stay_private():
                client.activity_generations(
                    tenant_id=tenant_id, tag_key="team", tag_value="search", start_at=START_AT,
                    limit=51,
                )
            sentry_sdk.capture_message("after the tag reads")
        private = "\n".join(transport.payloads)
        assert "after the tag reads" in private and "tag-reads" in private
        assert "param_" not in private and "search" not in private

        transport.payloads.clear()
        with sentry_sdk.start_transaction(name="control"):
            client.activity_generations(
                tenant_id=tenant_id, tag_key="team", tag_value="search", start_at=START_AT, limit=51
            )
        assert "param_tag_value=search" in "\n".join(transport.payloads)
    sentry.close()


@pytest.mark.parametrize("status", [500, 503])
def test_a_clickhouse_error_does_not_name_the_bound_values(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["param_tag_value"] == "zq-private-value"
        return httpx.Response(status, text="Code: 241. DB::Exception: Memory limit exceeded")

    client = _client(handler)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        client.activity_tag_breakdown(
            tenant_id="t", group_by="zq-private-group", start_at=START_AT, limit=50,
            tag_key="zq-private-key", tag_value="zq-private-value",
        )

    error = raised.value
    # httpx's own message would carry the whole request URL.
    assert "zq-private" not in str(error) and "param_" not in str(error)
    assert error.__cause__ is None and error.__suppress_context__
    assert error.response.status_code == status
    assert is_analytics_unavailable(error) is (status == 503)
    with caplog.at_level(logging.DEBUG):
        log_analytics_unavailable(logging.getLogger("tests.activity_tags"), "tag_test", error)
    [record] = caplog.records
    assert "zq-private" not in record.getMessage()
    assert "zq-private" not in str(record.__dict__)


def test_the_plain_row_reader_does_name_them_which_is_why_the_guard_exists() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(httpx.HTTPStatusError) as raised:
        _client(handler).activity_generations(
            tenant_id="t", tag_key="team", tag_value="zq-private-value", limit=1
        )
    assert "zq-private-value" in str(raised.value)

    store = SpannerStore.__new__(SpannerStore)
    store._operational_analytics = _client(handler)
    with pytest.raises(httpx.HTTPStatusError) as guarded:
        store.activity_tag_events(
            "ws", start_at=START_AT, tag_key="team", tag_value="zq-private-value"
        )
    assert "zq-private-value" not in str(guarded.value)
