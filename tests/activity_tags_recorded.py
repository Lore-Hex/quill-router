"""Synthetic tagged generations and what ClickHouse answered about them.

The durable stores answer the console's tag panel in ClickHouse and the
in-memory store answers it in Python. ``tests/fixtures/activity_tags_clickhouse.json``
is a transcript: every statement the tag reads sent to a real ClickHouse
holding ``FIXTURE``, with the rows that came back. The parity tests replay it
through the real reader and require the in-memory store to agree, so the two
implementations are held to the database's own behaviour (deduplication under
FINAL, grouping, ranking, hashing, the sketches) and not to a second Python
model of it. Each answer is recorded under the exact text of its statement,
so a changed query has no answer until it is recorded again.

To record again after changing a query, start the pinned server and run the
recorder against it. It needs an account that may create a database; it
creates ``tags_recording`` from this repository's single-node schema files,
inserts ``FIXTURE`` and runs every case below through the reader::

    docker run -d --name tags-recording -p 127.0.0.1:18123:8123 \\
        -e CLICKHOUSE_USER=recorder -e CLICKHOUSE_PASSWORD=recorder \\
        -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 clickhouse/clickhouse-server:26.8.36.3
    uv run python -m tests.activity_tags_recorded http://127.0.0.1:18123 recorder recorder

Every workspace, tag and number here is invented.
"""

from __future__ import annotations

import contextlib
import email
import hashlib
import json
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx

from trusted_router import storage_activity_tags
from trusted_router.operational_analytics import OperationalAnalyticsClient
from trusted_router.storage_models import Generation
from trusted_router.storage_operational_analytics import activity_payload, analytics_surrogate
from trusted_router.types import UsageType

ROOT = Path(__file__).resolve().parents[1]
TRANSCRIPT = ROOT / "tests" / "fixtures" / "activity_tags_clickhouse.json"
RECORDING_DATABASE = "tags_recording"

WORKSPACE = "ws-tags-a"
OTHER_WORKSPACE = "ws-tags-b"
EMPTY_WORKSPACE = "ws-tags-none"
START_AT = "2026-03-01T00:00:00Z"

# id, workspace, created_at, tags, cost, usage type, prompt, completion, elapsed.
# g04 has no latency, g06 is BYOK, g08 carries an empty value, g09 and g10 lack
# the team key, g11 differs from "search" only by case, g12 is one millisecond
# before the window and g13 belongs to another workspace.
#
# The team values are built so that each rule of the ranking decides a cut:
#   search 1350, billing 800, ads 700 (in credits), "" 20, zeta 10, Search 10,
#   delta 10
# billing outranks ads on cost although ads has more requests (limit 2), and
# zeta outranks Search and delta on requests at equal cost (limit 5).
#
# The elapsed times put the middle of a group at one value (search), at two
# values of one histogram slot (ads), in two slots (the untagged rows), at
# the tag histogram's ceiling of 4,194,304 ms, where no median is reported
# (billing), and one millisecond below it, where one is (delta).
FIXTURE: list[tuple[str, str, str, dict[str, str], int, str, int, int, int | None]] = [
    ("g01", WORKSPACE, "2026-03-02T10:00:00.000Z", {"team": "search", "environment": "production"}, 900, "Credits", 100, 50, 400),  # noqa: E501
    ("g02", WORKSPACE, "2026-03-02T11:00:00.000Z", {"team": "search", "environment": "production"}, 300, "Credits", 40, 20, 200),  # noqa: E501
    ("g03", WORKSPACE, "2026-03-03T09:00:00.250Z", {"team": "search", "environment": "staging"}, 100, "Credits", 30, 10, 600),  # noqa: E501
    ("g04", WORKSPACE, "2026-03-03T09:30:00.000Z", {"team": "search"}, 50, "Credits", 20, 5, None),
    ("g05", WORKSPACE, "2026-03-04T08:00:00.000Z", {"team": "ads", "environment": "production"}, 700, "Credits", 70, 30, 150),  # noqa: E501
    ("g06", WORKSPACE, "2026-03-04T08:05:00.000Z", {"team": "ads"}, 500, "BYOK", 60, 25, 300),
    ("g07", WORKSPACE, "2026-03-04T12:00:00.000Z", {"team": "billing"}, 800, "Credits", 80, 40, 4_194_304),  # noqa: E501
    ("g08", WORKSPACE, "2026-03-05T01:00:00.000Z", {"team": ""}, 20, "Credits", 5, 5, 1000),
    ("g09", WORKSPACE, "2026-03-05T02:00:00.000Z", {}, 40, "Credits", 8, 4, 300),
    ("g10", WORKSPACE, "2026-03-05T03:00:00.000Z", {"environment": "production"}, 60, "Credits", 9, 3, 90_000),  # noqa: E501
    ("g11", WORKSPACE, "2026-03-05T04:00:00.000Z", {"team": "Search"}, 10, "Credits", 2, 1, 700),
    ("g12", WORKSPACE, "2026-02-28T23:59:59.999Z", {"team": "search", "environment": "production"}, 9999, "Credits", 999, 999, 5),  # noqa: E501
    ("g13", OTHER_WORKSPACE, "2026-03-02T10:30:00.000Z", {"team": "other-workspace", "environment": "production"}, 8888, "Credits", 888, 888, 7),  # noqa: E501
    ("g14", WORKSPACE, "2026-03-05T05:00:00.000Z", {"team": "zeta"}, 10, "Credits", 3, 2, 800),
    ("g15", WORKSPACE, "2026-03-05T06:00:00.000Z", {"team": "zeta"}, 0, "Credits", 1, 1, 900),
    ("g16", WORKSPACE, "2026-03-05T07:00:00.000Z", {"team": "delta"}, 10, "Credits", 4, 2, 4_194_303),
]
# An earlier delivery of g02 that the delivery above replaced. ClickHouse
# holds both rows and FINAL keeps the later one; the in-memory store keeps
# the later one because it was added later. A read without FINAL would count
# this row too, as a second "ads" request with its own cost.
REPLACED: tuple[str, str, str, dict[str, str], int, str, int, int, int | None] = (
    "g02", WORKSPACE, "2026-03-02T11:00:00.000Z", {"team": "ads"}, 111, "Credits", 7, 7, 7,
)

# Each case is one store call. ``limits`` are the module limits in force for
# it, lowered so that a sixteen-row fixture reaches every cut.
SUMMARY_CASES: list[tuple[str, str, dict[str, Any], dict[str, int]]] = [
    ("default", WORKSPACE, {}, {}),
    ("filter", WORKSPACE, {"group_by": "environment", "tag_key": "team", "tag_value": "search"}, {}),
    ("key-only-filter", WORKSPACE, {"group_by": "team", "tag_key": "team"}, {}),
    ("empty-value", WORKSPACE, {"group_by": "team", "tag_key": "team", "tag_value": ""}, {}),
    ("absent-key", WORKSPACE, {"group_by": "cost-center"}, {}),
    ("other-workspace", OTHER_WORKSPACE, {}, {}),
    ("no-tags", EMPTY_WORKSPACE, {"group_by": "team", "tag_key": "team", "tag_value": "search"}, {}),
    ("cut-by-cost", WORKSPACE, {"group_by": "team"}, {"TAG_VALUE_LIMIT": 2}),
    ("cut-by-requests", WORKSPACE, {"group_by": "team"}, {"TAG_VALUE_LIMIT": 5}),
    ("keys-cut", WORKSPACE, {"group_by": "environment"}, {"TAG_KEY_LIMIT": 1}),
    ("values-cut", WORKSPACE, {"group_by": "environment", "tag_key": "team"}, {"TAG_VALUE_LIMIT": 2}),
    ("too-many-values", WORKSPACE, {"group_by": "team"}, {"TAG_DISTINCT_LIMIT": 6}),
    ("default-skips-too-many", WORKSPACE, {}, {"TAG_DISTINCT_LIMIT": 6}),
    ("every-default-too-many", WORKSPACE, {}, {"TAG_DISTINCT_LIMIT": 1}),
]
EVENT_CASES: list[tuple[str, dict[str, Any]]] = [
    ("key-and-value", {"tag_key": "team", "tag_value": "search"}),
    ("key-only", {"tag_key": "environment"}),
    ("empty-value", {"tag_key": "team", "tag_value": ""}),
    ("cut", {"tag_key": "team", "limit": 3}),
    ("no-match", {"tag_key": "cost-center"}),
]


def _generation(
    row: tuple[str, str, str, dict[str, str], int, str, int, int, int | None],
) -> Generation:
    generation_id, workspace, created_at, tags, cost, usage_type, prompt, completion, elapsed = row
    return Generation(
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
        usage_type=UsageType.coerce(usage_type),
        speed_tokens_per_second=10.0,
        finish_reason="stop",
        status="success",
        streamed=False,
        elapsed_milliseconds=elapsed,
        tags=dict(tags),
        created_at=created_at,
    )


def fixture_generations() -> list[Generation]:
    """Every delivery in order: the replaced one first, so the last one wins."""
    return [_generation(REPLACED), *(_generation(row) for row in FIXTURE)]


@contextlib.contextmanager
def limits(overrides: dict[str, int]) -> Iterator[None]:
    """Lower the tag module's limits for one case, as the recorder did."""
    saved = {name: getattr(storage_activity_tags, name) for name in overrides}
    try:
        for name, value in overrides.items():
            setattr(storage_activity_tags, name, value)
        yield
    finally:
        for name, value in saved.items():
            setattr(storage_activity_tags, name, value)


def form_fields(request: httpx.Request) -> dict[str, str]:
    """The fields of a multipart request body, by name."""
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        return {}
    message = email.message_from_bytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + request.content
    )
    fields: dict[str, str] = {}
    parts: list[Any] = list(message.get_payload())
    for part in parts:
        name = part.get_param("name", header="content-disposition")
        fields[str(name)] = part.get_payload(decode=True).decode()
    return fields


def statement(request: httpx.Request) -> tuple[str, dict[str, str]]:
    """The SQL and the bound values of one reader request, wherever they travel."""
    fields = form_fields(request)
    sql = fields.pop("query") if "query" in fields else request.content.decode()
    bound = {
        name.removeprefix("param_"): value
        for name, value in [*request.url.params.items(), *fields.items()]
        if name.startswith("param_")
    }
    return sql, bound


def exchange_key(sql: str, bound: dict[str, str]) -> str:
    """What identifies a recorded answer: the statement and its bound values.

    The tag reads are identified by their exact text, so a changed query has
    no answer until it is recorded again. The row read that predates the tag
    panel is identified by name: its text belongs to other changes, and its
    answer stays valid for as long as the rows it returns can be parsed.
    """
    name = read_name(sql)
    text = name if name == "generations" else hashlib.sha256(sql.encode()).hexdigest()
    return text + " " + json.dumps(bound, sort_keys=True, ensure_ascii=False)


def read_name(sql: str) -> str:
    """Which reader statement this is, from the text only it contains."""
    for name, marker in (
        ("keys", "arrayJoin(mapKeys(tags))"),
        ("values", "AS tag_value"),
        ("cardinality", "uniqCombinedIf"),
        ("ranking", "count() OVER () AS value_count"),
        ("names", "AS named_value"),
        ("histogram", "AS generations_at_value"),
        ("generations", "generation_id, request_id"),
    ):
        if marker in sql:
            return name
    raise AssertionError("a statement the tag reads do not send")


def load_transcript() -> dict[str, list[dict[str, Any]]]:
    recorded = json.loads(TRANSCRIPT.read_text())
    return {item["statement"]: item["rows"] for item in recorded["exchanges"]}


def recorded_handler(
    requests: list[httpx.Request],
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer each statement with what ClickHouse answered when it was recorded."""
    answers = load_transcript()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        sql, bound = statement(request)
        key = exchange_key(sql, bound)
        if key not in answers:
            raise AssertionError(
                f"no recorded answer for this {read_name(sql)} read: record the transcript again "
                "(tests/activity_tags_recorded.py)"
            )
        return httpx.Response(200, json={"data": answers[key]})

    return handler


def recorded_client(requests: list[httpx.Request]) -> OperationalAnalyticsClient:
    return OperationalAnalyticsClient(
        base_url="http://clickhouse.test:8123",
        user="tr_control_read",
        password="test-reader",  # noqa: S106 - inert test credential.
        transport=httpx.MockTransport(recorded_handler(requests)),
    )


def sql_store(client: OperationalAnalyticsClient) -> Any:
    from trusted_router.storage_gcp import SpannerStore

    store = SpannerStore.__new__(SpannerStore)
    store._operational_analytics = client
    return store


def reader_statements() -> dict[str, tuple[str, dict[str, str]]]:
    """One statement of each tag read, as the reader sends it today."""
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
    window: dict[str, Any] = {"tenant_id": analytics_surrogate("workspace", WORKSPACE), "start_at": START_AT}
    histogram: dict[str, Any] = {
        "group_by": "team",
        "top": [(1, 2), (3, 4)],
        "byok_hashes": [7],
        "opened": [("value", 1, 2, 3), ("other", 0, 0, 5)],
    }
    names = ["keys", "values"]
    client.activity_tag_keys(**window)
    client.activity_tag_values(tag_key="team", **window)
    # Each breakdown read has three texts: no filter, a key, a key and a value.
    filters: list[tuple[str, dict[str, Any]]] = [
        ("", {}),
        ("-by-key", {"tag_key": "environment"}),
        ("-by-tag", {"tag_key": "environment", "tag_value": "production"}),
    ]
    for suffix, tag in filters:
        client.activity_tag_cardinality(group_by="team", **window, **tag)
        client.activity_tag_ranking(group_by="team", limit=50, byok_hashes=[7], **window, **tag)
        client.activity_tag_names(group_by="team", top=[(1, 2)], **window, **tag)
        client.activity_tag_histogram(**histogram, **window, **tag)
        names += [name + suffix for name in ("cardinality", "ranking", "names", "histogram")]
    return {name: statement(request) for name, request in zip(names, sent, strict=True)}


# --------------------------------------------------------------- recording


def _schema_statements() -> list[str]:
    """This repository's single-node DDL for activity_generations, in order."""
    statements: list[str] = []
    for path in sorted((ROOT / "clickhouse").glob("*single_node*.sql")):
        text = re.sub(r"--[^\n]*", "", path.read_text())
        for part in text.split(";"):
            if re.search(r"\b(CREATE TABLE IF NOT EXISTS|ALTER TABLE) activity_generations\b", part):
                statements.append(part.strip())
    return statements


def _insert_rows(
    rows: list[tuple[str, str, str, dict[str, str], int, str, int, int, int | None]],
    ingested: str,
) -> str:
    lines = []
    for row in rows:
        payload = activity_payload(_generation(row))
        payload["ingest_version"] = ingested
        lines.append(json.dumps(payload))
    return "\n".join(lines)


def record(url: str, user: str, password: str) -> None:  # pragma: no cover - needs a server
    admin = httpx.Client(auth=(user, password), timeout=60)

    def run(sql: str, **params: str) -> None:
        response = admin.post(url, params=params, content=sql.encode())
        if response.status_code != 200:
            raise RuntimeError(response.text[:400])

    version = admin.post(url, content=b"SELECT version()").text.strip()
    run(f"DROP DATABASE IF EXISTS {RECORDING_DATABASE}")
    run(f"CREATE DATABASE {RECORDING_DATABASE}")
    for ddl in _schema_statements():
        run(ddl, database=RECORDING_DATABASE)
    # The fixture is older than the table's retention; keep a merge from
    # dropping it, and keep both deliveries of g02 until FINAL reads them.
    run("ALTER TABLE activity_generations REMOVE TTL", database=RECORDING_DATABASE)
    run("SYSTEM STOP MERGES activity_generations", database=RECORDING_DATABASE)
    # Two inserts, so two parts: rows with one sort key in a single insert
    # are collapsed as they are written, and FINAL would have nothing to do.
    for rows, ingested in (
        ([REPLACED], "2026-03-10 00:00:00.000000"),
        (FIXTURE, "2026-03-10 00:00:01.000000"),
    ):
        run(
            "INSERT INTO activity_generations FORMAT JSONEachRow\n" + _insert_rows(rows, ingested),
            database=RECORDING_DATABASE,
            input_format_skip_unknown_fields="1",
            date_time_input_format="best_effort",
        )
    stored = admin.post(
        url, params={"database": RECORDING_DATABASE}, content=b"SELECT count() FROM activity_generations"
    ).text.strip()
    if stored != str(len(FIXTURE) + 1):
        raise RuntimeError(f"expected both deliveries of g02 to be stored, found {stored} rows")

    exchanges: dict[str, dict[str, Any]] = {}
    network = httpx.HTTPTransport()

    def handler(request: httpx.Request) -> httpx.Response:
        response = network.handle_request(request)
        response.read()
        sql, bound = statement(request)
        if response.status_code != 200:
            raise RuntimeError(f"{read_name(sql)}: {response.text[:400]}")
        key = exchange_key(sql, bound)
        exchanges[key] = {"read": read_name(sql), "statement": key, "rows": response.json()["data"]}
        return httpx.Response(200, content=response.content)

    client = OperationalAnalyticsClient(
        base_url=url,
        user=user,
        password=password,
        database=RECORDING_DATABASE,
        transport=httpx.MockTransport(handler),
    )
    store = sql_store(client)
    for _name, workspace, kwargs, overrides in SUMMARY_CASES:
        with limits(overrides):
            store.activity_tag_summary(workspace, start_at=START_AT, **kwargs)
    for _name, kwargs in EVENT_CASES:
        store.activity_tag_events(WORKSPACE, start_at=START_AT, **kwargs)
    TRANSCRIPT.write_text(
        json.dumps(
            {
                "clickhouse_version": version,
                "recorded_with": "python -m tests.activity_tags_recorded",
                "exchanges": sorted(
                    exchanges.values(), key=lambda item: (item["read"], item["statement"])
                ),
            },
            indent=1,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    )
    run(f"DROP DATABASE {RECORDING_DATABASE}")
    print(f"recorded {len(exchanges)} statements from ClickHouse {version} into {TRANSCRIPT}")


if __name__ == "__main__":  # pragma: no cover
    record(*sys.argv[1:4])
