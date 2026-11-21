"""Run the shipped ``activity_generations`` SELECTs on fixture rows, in SQLite.

The unit suite has no ClickHouse. This stand-in executes the SQL text the
client really sends, so a wrong predicate, grouping key, bucket label or a
dropped ``FINAL`` shows up as a wrong result. Only what SQLite spells
differently is rewritten:

* ``{name:Type}`` placeholders become ``:name``. Values stay bound.
* ``FORMAT JSON`` is dropped. Rows go back in ClickHouse's JSON envelope.
* ``FROM activity_generations FINAL`` reads a view that keeps, for each sort
  key ``(tenant_id, created_at, generation_id)``, the row with the highest
  ``ingest_version``: what a ReplacingMergeTree returns under FINAL. The same
  text without FINAL reads the raw table, replayed deliveries included.
* ``quantileExactLow(0.5)(x)`` and ``quantileExactHigh(0.5)(x)`` become plain
  aggregates, and ``tags[key]`` a function call.

The ClickHouse functions those queries call are registered with ClickHouse's
meaning, including the trap that ``%M`` in ``formatDateTime`` is the month
name and ``%i`` the minute.

What a green run does not prove: that ClickHouse evaluates the SQL the same
way. That takes a ClickHouse server.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import statistics
from typing import Any

import httpx

from trusted_router.operational_analytics import OperationalAnalyticsClient
from trusted_router.storage_models import Generation
from trusted_router.storage_operational_analytics import activity_payload

_TEMPLATE = Generation(
    id="template",
    request_id="template",
    workspace_id="template",
    key_hash="template",
    model="template",
    provider_name="template",
    app="template",
    tokens_prompt=0,
    tokens_completion=0,
    total_cost_microdollars=0,
    usage_type="Credits",
    speed_tokens_per_second=0.0,
    finish_reason="stop",
    status="success",
    streamed=False,
)
_COLUMNS = (*activity_payload(_TEMPLATE), "ingest_version")
_JSON_COLUMNS = ("app_categories", "tags")
_PLACEHOLDER = re.compile(r"\{(\w+):(\w+)\}")
_FORMAT_TOKENS = {
    "%Y": "%Y",
    "%m": "%m",
    "%d": "%d",
    "%H": "%H",
    "%i": "%M",  # ClickHouse: minute.
    "%M": "%B",  # ClickHouse: month name, not minute.
}


def _moment(text: str) -> dt.datetime:
    """Parse the ISO forms the writers and readers use. A bare value is UTC."""
    value = dt.datetime.fromisoformat(str(text).replace(" ", "T").replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _stored(value: dt.datetime) -> str:
    """How ClickHouse renders a ``DateTime64(3, 'UTC')``. Sorts as text."""
    return value.strftime("%Y-%m-%d %H:%M:%S.") + f"{value.microsecond // 1000:03d}"


def format_datetime(text: str, pattern: str, timezone: str) -> str:
    """ClickHouse ``formatDateTime`` for the tokens the readers use."""
    if timezone != "UTC":
        raise ValueError(f"formatDateTime timezone must be UTC, got {timezone!r}")
    moment = _moment(text)
    parts = re.split(r"(%.)", pattern)
    for index, part in enumerate(parts):
        if part.startswith("%"):
            if part not in _FORMAT_TOKENS:
                raise ValueError(f"formatDateTime token {part} is not modelled")
            parts[index] = moment.strftime(_FORMAT_TOKENS[part])
    return "".join(parts)


def _start_of_five_minutes(text: str) -> str:
    moment = _moment(text)
    return _stored(moment.replace(minute=moment.minute // 5 * 5, second=0, microsecond=0))


class _Median:
    """``quantileExactLow(0.5)`` / ``quantileExactHigh(0.5)``: NULLs are skipped."""

    pick = staticmethod(statistics.median_low)

    def __init__(self) -> None:
        self.values: list[int] = []

    def step(self, value: int | None) -> None:
        if value is not None:
            self.values.append(value)

    def finalize(self) -> Any:
        # None, not zero, when no row carried a value (SQL NULL).
        return self.pick(self.values) if self.values else None


class _MedianHigh(_Median):
    pick = staticmethod(statistics.median_high)


class ActivityClickHouse:
    """An in-memory ``activity_generations`` table behind the real reader client."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.params: list[dict[str, str]] = []
        self._versions = 0
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE activity_generations ("
            + ", ".join(f'"{column}"' for column in _COLUMNS)
            + ")"
        )
        # The table's sort key. It keeps the FINAL view an index lookup per row.
        self.connection.execute(
            "CREATE INDEX activity_sort_key ON activity_generations "
            "(tenant_id, created_at, generation_id, ingest_version)"
        )
        self.connection.execute(
            """
CREATE VIEW activity_generations_final AS
SELECT * FROM activity_generations AS kept
WHERE NOT EXISTS (
  SELECT 1 FROM activity_generations AS newer
  WHERE newer.tenant_id = kept.tenant_id
    AND newer.created_at = kept.created_at
    AND newer.generation_id = kept.generation_id
    AND newer.ingest_version > kept.ingest_version
)
"""
        )
        create = self.connection.create_function
        create("formatDateTime", 3, format_datetime)
        create("toStartOfFiveMinutes", 1, _start_of_five_minutes)
        create("parseDateTime64BestEffort", 2, lambda text, _scale: _stored(_moment(text)))
        create("toDate", 1, lambda text: _stored(_moment(text))[:10])
        create("mapContains", 2, lambda tags, key: int(key in json.loads(tags)))
        create("mapValue", 2, lambda tags, key: json.loads(tags).get(key, ""))
        self.connection.create_aggregate("median_low", 1, _Median)
        self.connection.create_aggregate("median_high", 1, _MedianHigh)

    def add(self, generation: Generation, **columns: Any) -> None:
        """Deliver one generation. Call again for the same one to replay it.

        Every delivery gets a later ``ingest_version``, as the ingester's
        commit timestamp does. ``columns`` overrides delivered values, for a
        replay that carries different ones.
        """
        self._versions += 1
        row = dict(activity_payload(generation), ingest_version=self._versions, **columns)
        row["created_at"] = _stored(_moment(row["created_at"]))
        for column in _JSON_COLUMNS:
            row[column] = json.dumps(row[column])
        self.connection.execute(
            "INSERT INTO activity_generations VALUES ("  # noqa: S608 - fixed column list
            + ", ".join("?" for _ in _COLUMNS)
            + ")",
            [row[column] for column in _COLUMNS],
        )

    def client(self) -> OperationalAnalyticsClient:
        return OperationalAnalyticsClient(
            base_url="http://clickhouse.test:8123",
            user="reader",
            password="test",  # noqa: S106 - inert test credential.
            transport=httpx.MockTransport(self.handle),
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        sql = request.content.decode()
        raw = {
            key.removeprefix("param_"): value
            for key, value in request.url.params.items()
            if key.startswith("param_")
        }
        self.queries.append(sql)
        self.params.append(raw)
        bound: dict[str, str | int] = {}
        for name, kind in _PLACEHOLDER.findall(sql):
            bound[name] = int(raw[name]) if "Int" in kind else raw[name]
        sql = _PLACEHOLDER.sub(r":\1", sql)
        sql = sql.replace("FROM activity_generations FINAL", "FROM activity_generations_final")
        sql = re.sub(r"quantileExactLow\(0\.5\)\(", "median_low(", sql)
        sql = re.sub(r"quantileExactHigh\(0\.5\)\(", "median_high(", sql)
        sql = re.sub(r"\btags\[(:\w+)\]", r"mapValue(tags, \1)", sql)
        sql = re.sub(r"\s*FORMAT JSON\s*$", "", sql)
        rows = [dict(row) for row in self.connection.execute(sql, bound)]
        for row in rows:
            for column in _JSON_COLUMNS:
                if column in row:
                    row[column] = json.loads(row[column])
        return httpx.Response(200, json={"data": rows})
