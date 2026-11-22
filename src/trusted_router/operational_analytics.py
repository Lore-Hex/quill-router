"""Read-only ClickHouse adapter for operational metadata."""

# ruff: noqa: S608
# Identifiers are fixed constants and every request value is server-bound.

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import Any, TypeVar

import httpx

from trusted_router.clickhouse_endpoints import (
    CONNECT_FAILURES,
    FAILOVER_STATUS_CODES,
    parse_endpoints,
    request_timeout,
    send_with_failover,
)
from trusted_router.client_reliability import tenant_client_reliability_summary
from trusted_router.storage_models import (
    Generation,
    ProviderBenchmarkSample,
    SyntheticProbeSample,
    SyntheticRollup,
)
from trusted_router.types import UsageType

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
T = TypeVar("T")
PUBLIC_SNAPSHOT_QUERY_TIMEOUT_SECONDS = 2.0
# How long one client stops trying after a read that no endpoint answered.
# An unreachable cluster costs every read the connect timeout of every
# endpoint, and a status page makes several reads in a row, so without this
# an outage turns a page that should say "unavailable" into one that times
# out. Short, so reads resume within seconds of the cluster coming back.
UNREACHABLE_HOLD_SECONDS = 10.0


class AnalyticsUnreachable(httpx.ConnectError):
    """No attempt was made: moments ago no endpoint of this client answered.

    A ``ConnectError`` because that is what the read would have raised had it
    been tried, so callers and ``is_analytics_unavailable`` treat both alike.
    """


def is_analytics_unavailable(exc: BaseException) -> bool:
    """True when ClickHouse could not answer at all, not when it answered wrongly.

    Covers every transport failure (connect/read timeouts, refused or reset
    connections, a server disconnect) and the gateway statuses that
    ``send_with_failover`` treats as "this endpoint is down". A 4xx, a plain
    500 or a malformed payload is a query, schema or code defect and stays an
    error. User-facing routes use this to degrade (an honest "unavailable"
    state or a 503) instead of answering 500 for an analytics outage.
    """
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in FAILOVER_STATUS_CODES
    return False


def log_analytics_unavailable(
    logger: logging.Logger, surface: str, exc: BaseException
) -> None:
    """One stable WARNING per degraded read.

    The message text is constant so the records group as a single signal in
    Cloud Logging and Sentry Logs. The outage itself stays visible where it
    belongs: /status.json reports status as unknown, peer monitors read that
    as a blind deployment, and the fleet snapshot-freshness check pages on a
    sustained outage. A per-request error issue would only repeat that.
    """
    logger.warning(
        "operational_analytics.unavailable surface=%s error_class=%s",
        surface,
        type(exc).__name__,
        extra={
            "surface": surface,
            "error_class": type(exc).__name__,
            "error_message": str(exc)[:500],
            "retryable": True,
        },
    )


def _identifier(value: str, *, label: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a ClickHouse identifier")
    return value


# The bucket label of a usage series, per granularity. Each expression yields
# exactly the string ``storage_activity.usage_bucket_key`` cuts from an ISO
# timestamp, always in UTC, so the SQL aggregate and the in-memory store name
# the same buckets. Fixed constants: nothing from a request reaches the SQL.
USAGE_BUCKET_EXPRESSIONS: dict[str, str] = {
    "minute": "formatDateTime(created_at, '%Y-%m-%dT%H:%i', 'UTC')",
    "5min": "formatDateTime(toStartOfFiveMinutes(created_at), '%Y-%m-%dT%H:%i', 'UTC')",
    "hour": "formatDateTime(created_at, '%Y-%m-%dT%H', 'UTC')",
    "day": "formatDateTime(created_at, '%Y-%m-%d', 'UTC')",
}


# The bounded elapsed-time histogram behind every exact median here (see
# ``elapsed_histogram_columns`` and ``settle_elapsed_median``). Slots are
# 1,024 ms wide and the last one is open-ended, so one group of rows holds at
# most 65,536 slots, plus 1,024 distinct values for each slot it opens,
# however many rows or distinct values the range has.
ELAPSED_SLOT_MS = 1024
ELAPSED_LAST_SLOT = 65_535
ELAPSED_CEILING_MS = ELAPSED_LAST_SLOT * ELAPSED_SLOT_MS
# The coarse read, the read of the middle slots, and two more for a range
# whose middle moved between them.
_ELAPSED_READS = 4


def elapsed_histogram_columns(column: str, opened: str) -> str:
    """The two SELECT expressions of the bounded elapsed-time histogram.

    ``column`` is the latency expression, nullable milliseconds. ``opened`` is
    a condition that is true for a row whose slot should be opened, which for
    one group of rows is ``intDiv(<column>, 1024) IN (<its middle slots>)``.
    Both are SQL text fixed in the caller's code, never request input.

    The query that selects these must also select ``count() AS
    generations_at_value`` and group by ``slot, elapsed_value`` (after any
    key of its own, such as a tag). ``slot`` is NULL for an untimed row.
    ``elapsed_value`` is the row's value inside an opened slot and NULL
    elsewhere. ``settle_elapsed_median`` reads one group's rows.
    """
    return (
        "  CASE\n"
        f"    WHEN {column} IS NULL THEN NULL\n"
        f"    WHEN {column} >= {ELAPSED_CEILING_MS} THEN {ELAPSED_LAST_SLOT}\n"
        f"    ELSE intDiv({column}, {ELAPSED_SLOT_MS})\n"
        "  END AS slot,\n"
        "  CASE\n"
        f"    WHEN {column} < {ELAPSED_CEILING_MS}\n"
        f"      AND {opened}\n"
        f"    THEN {column}\n"
        "  END AS elapsed_value"
    )


def usage_buckets_query(*, granularity: str, by_model: bool, key_filtered: bool) -> str:
    """The usage-series aggregate: totals per time bucket, or per bucket and model.

    ClickHouse groups the rows instead of sending them to the caller, so the
    result is small whatever the tenant's volume, and nothing is cut off.
    Its row count does depend on how many distinct spellings of the usage
    type are stored, normally a handful because writers canonicalise it.
    FINAL stays: it is what counts a redelivered generation once. Every value
    is a bound parameter; the only variable text is the bucket expression,
    chosen from a fixed table by granularity.

    The stored usage type is a grouping key, not a condition: the caller
    decides Credits or BYOK for each stored value with ``UsageType.coerce``,
    the same function the row-by-row aggregation used, so the two cannot
    disagree on a value however it is spelled.
    """
    bucket = USAGE_BUCKET_EXPRESSIONS.get(granularity)
    if bucket is None:
        raise ValueError(f"unknown granularity: {granularity}")
    group = "bucket, model, usage_type" if by_model else "bucket, usage_type"
    # No alias repeats a column name: ClickHouse resolves a name inside an
    # aggregate to the alias first, which would nest the aggregate in itself.
    select = [f"{bucket} AS bucket"]
    if by_model:
        select.append("model")
    select += [
        "usage_type",
        "count() AS requests",
        "sum(tokens_prompt) AS prompt_tokens",
        "sum(tokens_completion) AS completion_tokens",
        "sum(reasoning_tokens) AS reasoning_token_total",
        "sum(total_cost_microdollars) AS cost_total",
    ]
    clauses = ["tenant_id = {tenant_id:String}"]
    if key_filtered:
        clauses.append("key_id = {key_id:String}")
    clauses.append("created_at >= parseDateTime64BestEffort({start_at:String}, 3)")
    clauses.append("created_at < parseDateTime64BestEffort({end_at:String}, 3)")
    return (
        "\nSELECT\n  "
        + ",\n  ".join(select)
        + "\nFROM activity_generations FINAL\nWHERE "
        + "\n  AND ".join(clauses)
        + f"\nGROUP BY {group}\nORDER BY {group}\nFORMAT JSON\n"
    )


def _iso(value: Any) -> str:
    text = str(value)
    if text.endswith("Z") or "+" in text[10:]:
        return text
    return text.replace(" ", "T") + "Z"


# Set while a read's bound values must stay out of the request URL.
_BOUND_VALUES_IN_BODY: ContextVar[bool] = ContextVar("clickhouse_bound_values_in_body", default=False)


@contextmanager
def bound_values_in_the_body() -> Iterator[None]:
    """Run a read whose bound values must stay out of what this process writes down.

    Tag keys and values are customer text that this service keeps out of its
    application logs and out of Sentry (docs/design/request-tagging.md,
    sections 6.5 and 10). ClickHouse normally takes bound values as URL
    parameters, and a URL is written down by more than one layer: httpx logs
    it at INFO, Sentry's httpx integration copies its query string into a
    span and a breadcrumb, and httpx names it in an ``HTTPStatusError``.

    Inside this scope ``_query`` sends the statement and every bound value as
    multipart form fields instead, which the ClickHouse HTTP interface reads
    the same way, so the URL carries only the database name. The Sentry scope
    without integrations and the status error without its URL remain as a
    second line: neither has anything to leak while the first holds.

    What this does not cover: ClickHouse records every statement, with its
    parameters substituted, in its own query log on the analytics servers.
    The values are there whichever way they were sent.
    """
    in_body = _BOUND_VALUES_IN_BODY.set(True)
    try:
        with _private_sentry_scope():
            try:
                yield
            except httpx.HTTPStatusError as exc:
                raise httpx.HTTPStatusError(
                    f"ClickHouse answered HTTP {exc.response.status_code} "
                    "to a read with private bound values",
                    request=exc.request,
                    response=exc.response,
                ) from None
    finally:
        _BOUND_VALUES_IN_BODY.reset(in_body)


@contextmanager
def _private_sentry_scope() -> Iterator[None]:
    try:
        import sentry_sdk
    except ImportError:
        # The ClickHouse node scripts import this module without the SDK, and
        # without the SDK there is no integration to keep the values from.
        yield
        return
    with sentry_sdk.new_scope() as scope:
        scope.set_client(_private_sentry_client())
        scope.span = None
        yield


@lru_cache(maxsize=1)
def _private_sentry_client() -> Any:
    import sentry_sdk

    # Same construction as services/resale.py: the explicitly empty DSN stops
    # the SENTRY_DSN environment fallback, and with no integrations there is
    # no outbound-request span, breadcrumb or SDK log to carry a query string.
    return sentry_sdk.Client(
        dsn="",
        default_integrations=False,
        auto_enabling_integrations=False,
        enable_logs=False,
        traces_sample_rate=0.0,
    )


class OperationalAnalyticsClient:
    """Fixed, parameterized reads over replicated ClickHouse tables."""

    def __init__(
        self,
        *,
        base_url: str,
        user: str,
        password: str,
        database: str = "tr",
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # One URL, or an ordered comma-separated list: see clickhouse_endpoints.
        self._endpoints = parse_endpoints(base_url)
        if not self._endpoints:
            raise ValueError("operational analytics ClickHouse URL is required")
        self._user = user
        self._password = password
        self._database = _identifier(database, label="database")
        self._transport = transport
        self._clock = clock
        self._unreachable_until = 0.0

    def _query(
        self,
        sql: str,
        *,
        params: dict[str, str | int] | None = None,
        timeout_seconds: float = 20.0,
        retry_read_disconnect: bool = False,
    ) -> list[dict[str, Any]]:
        if self._clock() < self._unreachable_until:
            raise AnalyticsUnreachable(
                "ClickHouse was unreachable moments ago; not trying again yet"
            )
        query_params: dict[str, str] = {"database": self._database}
        bound = {f"param_{key}": str(value) for key, value in (params or {}).items()}
        in_body = _BOUND_VALUES_IN_BODY.get()
        if not in_body:
            query_params.update(bound)
        answered = False

        def note_answer(_response: httpx.Response) -> None:
            nonlocal answered
            answered = True

        with httpx.Client(
            auth=(self._user, self._password),
            timeout=request_timeout(self._endpoints, timeout_seconds),
            transport=self._transport,
            event_hooks={"response": [note_answer]},
        ) as client:
            try:
                response = send_with_failover(
                    client,
                    self._endpoints,
                    lambda endpoint: (
                        # Form fields without a file name: ClickHouse reads
                        # "query" and every "param_*" field as it reads them
                        # from the URL.
                        client.build_request(
                            "POST",
                            endpoint,
                            params=query_params,
                            files={
                                "query": (None, sql),
                                **{name: (None, value) for name, value in bound.items()},
                            },
                        )
                        if in_body
                        else client.build_request(
                            "POST",
                            endpoint,
                            params=query_params,
                            content=sql,
                            headers={"content-type": "text/plain; charset=utf-8"},
                        )
                    ),
                    retry_read_disconnect=retry_read_disconnect,
                )
            except CONNECT_FAILURES:
                # The last endpoint refused a connection. If none before it
                # answered either, this says nothing about the query and
                # everything about the cluster, and the next read would wait
                # the same seconds for the same answer. An endpoint that
                # answered, even with a 503, is reachable, and the next read
                # may find it well. One that took the query and hung up
                # without a response has not answered.
                if not answered:
                    self._unreachable_until = self._clock() + UNREACHABLE_HOLD_SECONDS
                raise
            response.raise_for_status()
            payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("ClickHouse response did not contain data rows")
        return [dict(row) for row in rows if isinstance(row, dict)]

    def benchmark_samples(
        self,
        *,
        date: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        limit: int = 1000,
    ) -> list[ProviderBenchmarkSample]:
        clauses: list[str] = []
        params: dict[str, str | int] = {"limit": max(1, limit)}
        if date is not None:
            clauses.append("toDate(created_at) = toDate({date:String})")
            params["date"] = date
        if provider is not None:
            clauses.append("provider = {provider:String}")
            params["provider"] = provider
        if model is not None:
            clauses.append("model = {model:String}")
            params["model"] = model
        where = " AND ".join(clauses) if clauses else "1"
        rows = self._query(
            """
SELECT
  id, model, provider, provider_name, status, usage_type, streamed,
  input_tokens, output_tokens, total_cost_microdollars,
  speed_tokens_per_second, elapsed_milliseconds,
  first_token_milliseconds, ttfb_milliseconds, finish_reason,
  error_type, error_status, error_message, region, source, app, created_at
FROM provider_benchmark_samples FINAL
WHERE """
            + where
            + """
ORDER BY created_at DESC
LIMIT {limit:UInt32}
FORMAT JSON
""",
            params=params,
        )
        return [_benchmark_sample(row) for row in rows]

    def balanced_benchmark_samples(
        self,
        *,
        cutoff: str | None,
        per_provider_limit: int,
        limit: int,
    ) -> list[ProviderBenchmarkSample]:
        """Read one provider-balanced window without application-side fanout."""
        params: dict[str, str | int] = {
            "per_provider_limit": max(1, per_provider_limit),
            "limit": max(1, limit),
        }
        where = "1"
        if cutoff is not None:
            where = "created_at >= parseDateTime64BestEffort({cutoff:String}, 3)"
            params["cutoff"] = cutoff
        rows = self._query(
            """
SELECT
  id, model, provider, provider_name, status, usage_type, streamed,
  input_tokens, output_tokens, total_cost_microdollars,
  speed_tokens_per_second, elapsed_milliseconds,
  first_token_milliseconds, ttfb_milliseconds, finish_reason,
  error_type, error_status, error_message, region, source, app, created_at
FROM
(
  SELECT *, row_number() OVER (
    PARTITION BY provider ORDER BY created_at DESC, id DESC
  ) AS provider_rank
  FROM provider_benchmark_samples FINAL
  WHERE """
            + where
            + """
)
WHERE provider_rank <= {per_provider_limit:UInt32}
ORDER BY created_at DESC, id DESC
LIMIT {limit:UInt32}
FORMAT JSON
""",
            params=params,
        )
        return [_benchmark_sample(row) for row in rows]

    def route_benchmark_samples(
        self,
        *,
        cutoff: str,
        per_route_limit: int,
        limit: int,
    ) -> list[ProviderBenchmarkSample]:
        """Read a bounded recent window for every synthetic provider/model route."""
        rows = self._query(
            """
SELECT
  id, model, provider, provider_name, status, usage_type, streamed,
  input_tokens, output_tokens, total_cost_microdollars,
  speed_tokens_per_second, elapsed_milliseconds,
  first_token_milliseconds, ttfb_milliseconds, finish_reason,
  error_type, error_status, error_message, region, source, app, created_at
FROM
(
  SELECT *, row_number() OVER (
    PARTITION BY provider, model ORDER BY created_at DESC, id DESC
  ) AS route_rank
  FROM provider_benchmark_samples FINAL
  WHERE source = 'synthetic'
    AND created_at >= parseDateTime64BestEffort({cutoff:String}, 3)
)
WHERE route_rank <= {per_route_limit:UInt32}
ORDER BY created_at DESC, id DESC
LIMIT {limit:UInt32}
FORMAT JSON
""",
            params={
                "cutoff": cutoff,
                "per_route_limit": max(1, per_route_limit),
                "limit": max(1, limit),
            },
        )
        return [_benchmark_sample(row) for row in rows]

    def account_refusal_samples(
        self, *, cutoff: str, per_route_limit: int, limit: int,
    ) -> list[ProviderBenchmarkSample]:
        """Recent Credits samples of every route a provider refused.

        A route qualifies with one HTTP 401/403 since the cutoff; its newest
        organic and probe Credits samples then come back whatever their
        outcome, so a success after the refusals ends the streak. Capped per
        route, so one busy refused route cannot crowd out a quiet one.
        """
        rows = self._query(
            """
SELECT
  id, model, provider, provider_name, status, usage_type, streamed,
  input_tokens, output_tokens, total_cost_microdollars,
  speed_tokens_per_second, elapsed_milliseconds,
  first_token_milliseconds, ttfb_milliseconds, finish_reason,
  error_type, error_status, error_message, region, source, app, created_at
FROM
(
  SELECT *, row_number() OVER (
    PARTITION BY provider, model ORDER BY created_at DESC, id DESC
  ) AS route_rank
  FROM provider_benchmark_samples FINAL
  WHERE source IN ('organic', 'synthetic')
    AND usage_type = 'Credits'
    AND created_at >= parseDateTime64BestEffort({cutoff:String}, 3)
    AND (provider, model) IN (
      SELECT provider, model FROM provider_benchmark_samples FINAL
      WHERE source IN ('organic', 'synthetic')
        AND usage_type = 'Credits'
        AND error_status IN (401, 403)
        AND created_at >= parseDateTime64BestEffort({cutoff:String}, 3)
    )
)
WHERE route_rank <= {per_route_limit:UInt32}
ORDER BY created_at DESC, id DESC
LIMIT {limit:UInt32}
FORMAT JSON
""",
            params={"cutoff": cutoff, "per_route_limit": max(1, per_route_limit), "limit": max(1, limit)},
        )
        return [_benchmark_sample(row) for row in rows]

    def route_workspaces(
        self, *, provider: str, model: str, start_at: str, end_at: str,
    ) -> list[str]:
        """Workspaces with a generation on this exact route in [start, end).

        Read the same activity table as /v1/activity, without its recent-row
        limit: a busy workspace must not hide another workspace's one call.
        Only workspace ids leave ClickHouse; no keys or content are selected.
        """
        rows = self._query(
            """
SELECT DISTINCT workspace_id
FROM activity_generations FINAL
WHERE provider = {provider:String} AND model = {model:String}
  AND created_at >= parseDateTime64BestEffort({start_at:String}, 3)
  AND created_at < parseDateTime64BestEffort({end_at:String}, 3)
  AND workspace_id != ''
ORDER BY workspace_id
FORMAT JSON
""",
            params={"provider": provider, "model": model, "start_at": start_at, "end_at": end_at},
        )
        return [str(row["workspace_id"]) for row in rows]

    def activity_generations(
        self,
        *,
        tenant_id: str,
        key_id: str | None = None,
        tag_key: str | None = None,
        tag_value: str | None = None,
        date: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        limit: int = 5001,
    ) -> list[Generation]:
        clauses = ["tenant_id = {tenant_id:String}"]
        params: dict[str, str | int] = {
            "tenant_id": tenant_id,
            "limit": max(1, limit),
        }
        if date is not None:
            clauses.append("toDate(created_at) = toDate({date:String})")
            params["date"] = date
        if key_id is not None:
            clauses.append("key_id = {key_id:String}")
            params["key_id"] = key_id
        if tag_key is not None:
            clauses.append("mapContains(tags, {tag_key:String})")
            params["tag_key"] = tag_key
            if tag_value is not None:
                clauses.append("tags[{tag_key:String}] = {tag_value:String}")
                params["tag_value"] = tag_value
        if start_at is not None:
            clauses.append("created_at >= parseDateTime64BestEffort({start_at:String}, 3)")
            params["start_at"] = start_at
        if end_at is not None:
            clauses.append("created_at < parseDateTime64BestEffort({end_at:String}, 3)")
            params["end_at"] = end_at
        rows = self._query(
            """
SELECT
  generation_id, request_id, key_id, model, requested_model, provider, provider_name, app, app_id,
  tokens_prompt, tokens_completion, cached_input_tokens, reasoning_tokens,
  total_cost_microdollars, app_markup_microdollars, custom_model_markup_microdollars,
  usage_type, speed_tokens_per_second,
  finish_reason, status, streamed, usage_estimated, elapsed_milliseconds,
  first_token_milliseconds, ttfb_milliseconds, region, user, session_id,
  http_referer, app_categories, tags, created_at
FROM activity_generations FINAL
WHERE """
            + " AND ".join(clauses)
            + """
ORDER BY created_at DESC
LIMIT {limit:UInt32}
FORMAT JSON
""",
            params=params,
        )
        return [_generation(row, tenant_id=tenant_id) for row in rows]

    def activity_usage_buckets(
        self,
        *,
        tenant_id: str,
        granularity: str,
        start_at: str,
        end_at: str,
        key_id: str | None = None,
        by_model: bool = False,
    ) -> list[dict[str, Any]]:
        """Usage totals of ``[start_at, end_at)`` per bucket, or per bucket and model.

        One row per bucket (and model when asked), in that order, carrying the
        six integers of ``storage_activity.generation_metrics``, summed.
        """
        params: dict[str, str | int] = {
            "tenant_id": tenant_id,
            "start_at": start_at,
            "end_at": end_at,
        }
        if key_id is not None:
            params["key_id"] = key_id
        rows = self._query(
            usage_buckets_query(
                granularity=granularity,
                by_model=by_model,
                key_filtered=key_id is not None,
            ),
            params=params,
        )
        return _usage_bucket_rows(rows, by_model=by_model)

    def activity_elapsed_summary(
        self,
        *,
        tenant_id: str,
        start_at: str,
    ) -> dict[str, int | None]:
        """Server-side elapsed time over every generation since ``start_at``.

        ``median_ms`` is the median of the rows that carry an elapsed time,
        over the whole range rather than a newest-rows sample. For an even
        count it is the floor of the mean of the two middle values, which is
        what the page showed when it computed the median itself.

        Memory is bounded whatever the range holds. An exact quantile function
        keeps every row's value, and a histogram of every distinct value
        keeps one entry per value in every thread; either runs a busy
        tenant's range into the reader's memory limit. This reads a coarse
        histogram instead: rows counted per slot of 1,024 ms, at most 65,536
        slots. That locates the one or two slots the middle values fall in. A
        second read counts the distinct values inside those slots only, at
        most 2,048, and the median is read off the running count. Each read
        is one statement over one snapshot and carries its own totals, so the
        answer is exact for the rows that read saw; if rows arriving between
        the two reads moved the middle to another slot, the read is repeated
        for that slot.

        The last slot is open-ended (about 18.6 hours and up) and is never
        opened: a median that long is reported as absent, not approximated.
        """
        slots = (ELAPSED_LAST_SLOT, ELAPSED_LAST_SLOT)  # the first read opens no slot
        for _ in range(_ELAPSED_READS):
            rows = self._query(
                "\nSELECT\n"
                + elapsed_histogram_columns(
                    "elapsed_milliseconds",
                    "intDiv(elapsed_milliseconds, 1024)"
                    " IN ({slot_low:UInt64}, {slot_high:UInt64})",
                )
                + """,
  count() AS generations_at_value
FROM activity_generations FINAL
WHERE tenant_id = {tenant_id:String}
  AND created_at >= parseDateTime64BestEffort({start_at:String}, 3)
GROUP BY slot, elapsed_value
FORMAT JSON
""",
                params={
                    "tenant_id": tenant_id,
                    "start_at": start_at,
                    "slot_low": slots[0],
                    "slot_high": slots[1],
                },
            )
            settled = settle_elapsed_median(rows)
            if isinstance(settled, dict):
                return settled
            slots = settled
        raise RuntimeError("elapsed median did not settle: the range kept changing between reads")

    def routing_traffic(
        self, *, start_at: dt.datetime, end_at: dt.datetime,
    ) -> list[dict[str, Any]]:
        """Settled organic traffic, not attempts or a prediction of eligibility.

        Keep unattributed rows so callers can report coverage. FINAL removes
        delivery replays. Resource limits throw rather than return partial totals.
        """
        if start_at.utcoffset() is None or end_at.utcoffset() is None:
            raise ValueError("routing traffic requires timezone-aware bounds")
        if not dt.timedelta(0) < end_at - start_at <= dt.timedelta(days=31):
            raise ValueError("routing traffic window must be positive and at most 31 days")
        rows = self._query(
            """
WITH throwIf(
  getSetting('readonly') != 1
  OR getSetting('max_execution_time') <= 0 OR getSetting('max_execution_time') > 30
  OR getSetting('max_memory_usage') <= 0 OR getSetting('max_memory_usage') > 1073741824
  OR getSetting('read_overflow_mode') != 'throw'
  OR getSetting('group_by_overflow_mode') != 'throw'
  OR getSetting('timeout_overflow_mode') != 'throw'
  OR getSetting('result_overflow_mode') != 'throw',
  'routing traffic requires a bounded read-only profile (30s, 1GiB, throw on overflow)'
) AS profile_check
SELECT
  toString(toDate(created_at, 'UTC')) AS day,
  requested_model, model AS served_model, provider, usage_type,
  count() AS generations,
  sum(tokens_prompt) AS input_tokens,
  sum(tokens_completion) AS output_tokens,
  sum(cached_input_tokens) AS cached_input_tokens,
  sum(total_cost_microdollars) AS customer_charge_microdollars,
  count(elapsed_milliseconds) AS latency_samples,
  sum(ifNull(elapsed_milliseconds, 0)) AS latency_total_ms,
  count(first_token_milliseconds) AS ttft_samples,
  sum(ifNull(first_token_milliseconds, 0)) AS ttft_total_ms,
  countIf(usage_estimated) AS estimated_usage_generations
FROM activity_generations FINAL
WHERE created_at >= parseDateTime64BestEffort({start_at:String}, 3)
  AND created_at < parseDateTime64BestEffort({end_at:String}, 3)
  AND synthetic = 0 AND status = 'success'
  AND profile_check = 0
GROUP BY day, requested_model, served_model, provider, usage_type
ORDER BY day, requested_model, served_model, provider, usage_type
LIMIT 100001
FORMAT JSON
""",
            params={
                "start_at": start_at.astimezone(dt.UTC).isoformat(),
                "end_at": end_at.astimezone(dt.UTC).isoformat(),
            },
            timeout_seconds=35.0,
        )
        if len(rows) > 100000:
            raise RuntimeError("routing traffic exceeded 100000 groups; narrow the window")
        return rows

    def client_reliability_summary(
        self,
        tenant_id_surrogate: str,
        *,
        window_minutes: int,
    ) -> dict[str, Any]:
        rows = self._query(
            """
SELECT
  period, host, endpoint, sdk, requests, successes, tr_fault_failures,
  excluded_failures, aborted, attempts, attempt_tr_fault, failover_used,
  first_attempt_success, total_ms_hist, first_event_ms_hist
FROM client_availability_rollups FINAL
WHERE scope = 'tenant'
  AND tenant_id = {tenant_id:String}
  AND period IN ('5m', 'hour')
  AND period_start >= now() - INTERVAL {window_minutes:UInt32} MINUTE
  AND endpoint = ''
  AND sdk = ''
ORDER BY period_start DESC, host
FORMAT JSON
""",
            params={
                "tenant_id": tenant_id_surrogate,
                "window_minutes": max(1, window_minutes),
            },
        )
        return tenant_client_reliability_summary(
            rows,
            window_minutes=max(1, window_minutes),
        )

    def client_events_recent(
        self,
        tenant_id_surrogate: str,
        *,
        since: dt.datetime,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=dt.UTC)
        else:
            since = since.astimezone(dt.UTC)
        normalized_limit = max(1, min(limit, 50))
        rows = self._query(
            """
SELECT
  created_at, endpoint, model, attempt_host, attempt_count, final_outcome,
  final_http_status, first_error_class, sdk, sdk_version, attempt_request_id
FROM client_request_events FINAL
WHERE tenant_id = {tenant_id:String}
  AND created_at >= parseDateTime64BestEffort({since:String}, 3)
  AND final_outcome != 'ok'
ORDER BY created_at DESC
LIMIT {limit:UInt32}
FORMAT JSON
""",
            params={
                "tenant_id": tenant_id_surrogate,
                "since": since.isoformat().replace("+00:00", "Z"),
                "limit": normalized_limit,
            },
        )
        return [_client_event(row) for row in rows]

    def synthetic_samples(
        self,
        *,
        date: str | None = None,
        target: str | None = None,
        probe_type: str | None = None,
        monitor_region: str | None = None,
        limit: int = 1000,
    ) -> list[SyntheticProbeSample]:
        clauses: list[str] = []
        params: dict[str, str | int] = {"limit": max(1, limit)}
        for column, value in (
            ("target", target),
            ("probe_type", probe_type),
            ("monitor_region", monitor_region),
        ):
            if value is not None:
                clauses.append(f"{column} = {{{column}:String}}")
                params[column] = value
        if date is not None:
            clauses.append("toDate(created_at) = toDate({date:String})")
            params["date"] = date
        where = " AND ".join(clauses) if clauses else "1"
        rows = self._query(
            "SELECT * EXCEPT ingest_version FROM synthetic_probe_samples FINAL "
            f"WHERE {where} ORDER BY created_at DESC LIMIT {{limit:UInt32}} FORMAT JSON",
            params=params,
        )
        return [_dataclass_from_row(SyntheticProbeSample, row) for row in rows]

    def synthetic_rollups(
        self,
        *,
        period: str | None = None,
        since: str | None = None,
        until: str | None = None,
        include_histograms: bool = True,
        limit: int = 1000,
    ) -> list[SyntheticRollup]:
        clauses: list[str] = []
        params: dict[str, str | int] = {"limit": max(1, limit)}
        if period is not None:
            clauses.append("period = {period:String}")
            params["period"] = period
        if since is not None:
            clauses.append("period_start >= parseDateTimeBestEffort({since:String})")
            params["since"] = since
        if until is not None:
            clauses.append("period_start <= parseDateTimeBestEffort({until:String})")
            params["until"] = until
        where = " AND ".join(clauses) if clauses else "1"
        rows = self._query(
            "SELECT * EXCEPT ingest_version FROM synthetic_status_rollups FINAL "
            f"WHERE {where} ORDER BY period_start DESC LIMIT {{limit:UInt32}} FORMAT JSON",
            params=params,
        )
        rollups = [_dataclass_from_row(SyntheticRollup, row) for row in rows]
        if not include_histograms:
            rollups = [
                dataclasses.replace(
                    rollup,
                    latency_histogram={},
                    ttfb_histogram={},
                    dns_histogram={},
                    tcp_connect_histogram={},
                    tls_handshake_histogram={},
                    gateway_processing_histogram={},
                )
                for rollup in rollups
            ]
        return rollups

    # Request tags for the console ------------------------------------------
    # Every read covers every generation of one tenant since ``start_at``, not
    # a sample, and holds a bounded amount of state however many rows, tag
    # values or latencies the range has. Each bound is in the statement that
    # needs it; none rests on what an earlier statement saw:
    #
    #   keys, values   a top-K sketch with a fixed number of counters
    #   candidates     two top-K sketches and one distinct-count sketch, with
    #                  no GROUP BY
    #   totals         one group per candidate, for the candidates named in
    #                  the statement and no other value
    #   histogram      at most 52 groups, each the bounded elapsed-time
    #                  histogram of ``elapsed_histogram_columns``, cut off at
    #                  TAG_ELAPSED_SLOTS slots
    #
    # Credits and BYOK are told apart inside each statement that sums cost,
    # by one fixed expression (``_TAG_IS_BYOK``) that makes two classes
    # whatever is stored: no statement groups by the stored usage type or
    # takes a list of usage types that an earlier one saw. Tag keys and
    # values are always bound, and inside ``bound_values_in_the_body`` they
    # travel in the request body.

    def activity_tag_keys(self, *, tenant_id: str, start_at: str) -> list[dict[str, Any]]:
        """Tag keys by the number of generations carrying them, unordered.

        ``topK`` keeps 10,000 counters. With no more distinct keys than that
        every count is exact and ``overcount`` is 0; beyond it the function
        still returns the most frequent keys, each with the most its count
        can be too high by.
        """
        with bound_values_in_the_body():
            rows = self._query(
                """
SELECT tupleElement(item, 1) AS tag_key, tupleElement(item, 2) AS requests, tupleElement(item, 3) AS overcount
FROM
(
  SELECT arrayJoin(topK(1000, 10, 'counts')(tag_key)) AS item
  FROM
  (
    SELECT arrayJoin(mapKeys(tags)) AS tag_key
    FROM activity_generations FINAL
    WHERE tenant_id = {tenant_id:String}
      AND created_at >= parseDateTime64BestEffort({start_at:String}, 3)
  )
)
FORMAT JSON
""",
                params={"tenant_id": tenant_id, "start_at": start_at},
            )
        return [
            {
                "key": str(row["tag_key"]),
                "requests": int(row["requests"]),
                "overcount": int(row["overcount"]),
            }
            for row in rows
        ]

    def activity_tag_values(
        self, *, tenant_id: str, tag_key: str, start_at: str
    ) -> list[dict[str, Any]]:
        """One key's values by the number of generations carrying them, unordered.

        The same sketch as the keys, with 5,000 counters: exact for a key
        with no more distinct values than that, the most frequent values with
        an ``overcount`` beyond it.
        """
        with bound_values_in_the_body():
            rows = self._query(
                """
SELECT tupleElement(item, 1) AS tag_value, tupleElement(item, 2) AS requests, tupleElement(item, 3) AS overcount
FROM
(
  SELECT arrayJoin(topK(500, 10, 'counts')(tags[{tag_key:String}])) AS item
  FROM activity_generations FINAL
  WHERE tenant_id = {tenant_id:String}
    AND created_at >= parseDateTime64BestEffort({start_at:String}, 3)
    AND mapContains(tags, {tag_key:String})
)
FORMAT JSON
""",
                params={"tenant_id": tenant_id, "tag_key": tag_key, "start_at": start_at},
            )
        return [
            {
                "value": str(row["tag_value"]),
                "requests": int(row["requests"]),
                "overcount": int(row["overcount"]),
            }
            for row in rows
        ]

    def activity_tag_candidates(
        self,
        *,
        tenant_id: str,
        group_by: str,
        start_at: str,
        candidates: int,
        tag_key: str | None = None,
        tag_value: str | None = None,
    ) -> dict[str, Any]:
        """Which values of ``group_by`` could keep a row, from fixed-size state.

        One pass with no GROUP BY, so its memory does not depend on how many
        distinct values the key has, in this read or in any other: about how
        many distinct values there are, the ``candidates`` values with the
        most cost, and the ``candidates`` values with the most generations.
        A value is named by a 64-bit hash here; its text is read by
        ``activity_tag_candidate_totals``.

        Each list holds ``(hash, weight, error)``. With fewer distinct values
        than ``candidates`` both lists hold every value this read saw. With
        more, they hold the heaviest values the sketches found. Cost here is
        every usage type's: the totals read separates Credits from BYOK.
        """
        where, params = _tag_window(tenant_id, start_at, tag_key, tag_value)
        with bound_values_in_the_body():
            rows = self._query(
                """
SELECT
  distinct_values,
  arrayMap(item -> [tupleElement(item, 1), tupleElement(item, 2), tupleElement(item, 3)], by_cost) AS by_cost,
  arrayMap(item -> [tupleElement(item, 1), tupleElement(item, 2), tupleElement(item, 3)], by_requests) AS by_requests
FROM
(
  SELECT
    uniqCombined(cityHash64(tags[{group_by:String}])) AS distinct_values,
    topKWeighted({candidates:UInt32}, 3, 'counts')(cityHash64(tags[{group_by:String}]), toUInt64(greatest(total_cost_microdollars, 0))) AS by_cost,
    topK({candidates:UInt32}, 3, 'counts')(cityHash64(tags[{group_by:String}])) AS by_requests
  FROM activity_generations FINAL
  WHERE """
                + where
                + """
    AND mapContains(tags, {group_by:String})
)
FORMAT JSON
""",
                params={**params, "group_by": group_by, "candidates": max(1, candidates)},
            )
        row = rows[0] if rows else {}

        def listed(name: str) -> list[tuple[int, int, int]]:
            return [(int(item[0]), int(item[1]), int(item[2])) for item in row.get(name) or []]

        return {
            "distinct_values": int(row.get("distinct_values") or 0),
            "by_cost": listed("by_cost"),
            "by_requests": listed("by_requests"),
        }

    def activity_tag_candidate_totals(
        self,
        *,
        tenant_id: str,
        group_by: str,
        start_at: str,
        candidates: list[int],
        tag_key: str | None = None,
        tag_value: str | None = None,
    ) -> list[dict[str, Any]]:
        """Text, generations and credit cost of the candidate values.

        Only values whose hash is in ``candidates`` are grouped, so this holds
        at most one group for each candidate whatever the key has become
        since the candidates were read. Two values that shared the first hash
        are still two groups here, told apart by the second; two that shared
        both would be one group under the smaller text.
        """
        where, params = _tag_window(tenant_id, start_at, tag_key, tag_value)
        with bound_values_in_the_body():
            rows = self._query(
                """
SELECT
  cityHash64(tags[{group_by:String}]) AS hash_a,
  sipHash64(tags[{group_by:String}]) AS hash_b,
  min(tags[{group_by:String}]) AS named_value,
  count() AS requests,
  sumIf(total_cost_microdollars, NOT """
                + _TAG_IS_BYOK
                + """) AS credit_microdollars
FROM activity_generations FINAL
WHERE """
                + where
                + """
  AND mapContains(tags, {group_by:String})
  AND cityHash64(tags[{group_by:String}]) IN {candidates:Array(UInt64)}
GROUP BY hash_a, hash_b
FORMAT JSON
""",
                params={**params, "group_by": group_by, "candidates": _integer_array(candidates)},
            )
        return [
            {
                "hash_a": int(row["hash_a"]),
                "hash_b": int(row["hash_b"]),
                "value": str(row["named_value"]),
                "requests": int(row["requests"]),
                "credit_microdollars": int(row["credit_microdollars"]),
            }
            for row in rows
        ]

    def activity_tag_histogram(
        self,
        *,
        tenant_id: str,
        group_by: str,
        start_at: str,
        top: list[tuple[int, int]],
        opened: list[tuple[str, int, int, int]],
        tag_key: str | None = None,
        tag_value: str | None = None,
    ) -> list[dict[str, Any]]:
        """Totals and the elapsed-time histogram for the rows a breakdown shows.

        A generation belongs to the ``value`` group of its tag value when the
        value's hashes are in ``top``, to ``other`` when it has another
        value, and to ``untagged`` without the key: at most 52 groups, one
        row each. Credits and BYOK are summed apart inside this statement, so
        a usage type that first appears after an earlier read is still told
        apart.

        Each group's elapsed times are counted with the histogram the page's
        own median uses (``elapsed_histogram_columns``): slots of 1,024 ms,
        and distinct values inside the slots named for that group in
        ``opened``, as ``(kind, hash_a, hash_b, slot)``. Its ``histogram`` is
        those counts as ``(slot, elapsed_value, generations_at_value)``, the
        three columns ``settle_elapsed_median`` reads.

        One group of the page's histogram can hold 65,536 slots, and 52 of
        those ran the reader out of its memory when measured. Here an elapsed
        time of TAG_ELAPSED_CEILING_MS or more is counted in the histogram's
        open-ended last slot, so a group holds at most 4,097 slots plus 1,024
        values for each slot it opens, whatever its range holds, and a median
        that long is reported as absent, as the page's is past its own
        ceiling.

        The outer query only packs each group's histogram into one array, so
        that a breakdown of many busy values crosses the wire as at most 52
        rows and not as one row for each count.
        """
        where, params = _tag_window(tenant_id, start_at, tag_key, tag_value)
        with bound_values_in_the_body():
            rows = self._query(
                """
SELECT
  kind, hash_a, hash_b,
  sum(generations_at_value) AS requests,
  sum(cost_at_value) AS cost_microdollars,
  sum(byok_at_value) AS byok_microdollars,
  sum(prompt_at_value) AS prompt_tokens,
  sum(completion_at_value) AS completion_tokens,
  groupArray(tuple(ifNull(toInt32(slot), -1), ifNull(toInt32(elapsed_value), -1), generations_at_value)) AS histogram
FROM
(
  SELECT
    multiIf(
      NOT mapContains(tags, {group_by:String}), 'untagged',
      (cityHash64(tags[{group_by:String}]), sipHash64(tags[{group_by:String}])) IN {top:Array(Tuple(UInt64, UInt64))}, 'value',
      'other'
    ) AS kind,
    if(kind = 'value', cityHash64(tags[{group_by:String}]), 0) AS hash_a,
    if(kind = 'value', sipHash64(tags[{group_by:String}]), 0) AS hash_b,
"""
                + elapsed_histogram_columns(
                    _TAG_ELAPSED_COLUMN,
                    "(kind, hash_a, hash_b, intDiv(elapsed_milliseconds, 1024))"
                    " IN {opened:Array(Tuple(String, UInt64, UInt64, UInt64))}",
                )
                + """,
    count() AS generations_at_value,
    sumIf(total_cost_microdollars, NOT """
                + _TAG_IS_BYOK
                + """) AS cost_at_value,
    sumIf(total_cost_microdollars, """
                + _TAG_IS_BYOK
                + """) AS byok_at_value,
    sum(tokens_prompt) AS prompt_at_value,
    sum(tokens_completion) AS completion_at_value
  FROM activity_generations FINAL
  WHERE """
                + where
                + """
  GROUP BY kind, hash_a, hash_b, slot, elapsed_value
)
GROUP BY kind, hash_a, hash_b
FORMAT JSON
""",
                params={
                    **params,
                    "group_by": group_by,
                    "top": _tuple_array(top),
                    "opened": _cell_array(opened),
                },
            )
        return [_tag_histogram_row(row) for row in rows]

    def public_snapshot(self, name: str) -> dict[str, Any] | None:
        if name not in {
            "leaderboard",
            "leaderboard_evidence",
            "apps",
            "video_leaderboard",
            "status_inputs",
            "client_reliability",
            "encrypted_ai",
            "model_routing_evidence",
        }:
            raise ValueError("unsupported public analytics snapshot")
        rows = self._query(
            """
SELECT payload
FROM public_analytics_snapshots FINAL
WHERE name = {name:String}
ORDER BY generated_at DESC
LIMIT 1
FORMAT JSON
""",
            params={"name": name},
            timeout_seconds=PUBLIC_SNAPSHOT_QUERY_TIMEOUT_SECONDS,
            retry_read_disconnect=True,
        )
        if not rows:
            return None
        payload = json.loads(str(rows[0].get("payload") or "{}"))
        return payload if isinstance(payload, dict) else None


def _dataclass_from_row(cls: type[T], row: dict[str, Any]) -> T:
    allowed = {field.name for field in dataclasses.fields(cls)}  # type: ignore[arg-type]
    payload = {key: value for key, value in row.items() if key in allowed}
    if cls is SyntheticProbeSample:
        for key in ("connection_reused", "output_match"):
            if payload.get(key) is not None:
                payload[key] = bool(payload[key])
    if cls is SyntheticRollup and payload.get("target_region") == "":
        payload["target_region"] = None
    for key in ("created_at", "period_start", "updated_at", "last_checked_at"):
        if payload.get(key) is not None:
            payload[key] = _iso(payload[key])
    return cls(**payload)


def _benchmark_sample(row: dict[str, Any]) -> ProviderBenchmarkSample:
    row = dict(row)
    row["created_at"] = _iso(row["created_at"])
    row["streamed"] = bool(row["streamed"])
    return _dataclass_from_row(ProviderBenchmarkSample, row)


def _generation(row: dict[str, Any], *, tenant_id: str) -> Generation:
    return Generation(
        id=str(row["generation_id"]),
        request_id=str(row["request_id"]),
        workspace_id=tenant_id,
        key_hash=str(row["key_id"]),
        model=str(row["model"]),
        requested_model=str(row.get("requested_model") or ""),
        provider_name=str(row["provider_name"]),
        app=str(row["app"]),
        app_id=str(row.get("app_id") or ""),
        tokens_prompt=int(row["tokens_prompt"]),
        tokens_completion=int(row["tokens_completion"]),
        total_cost_microdollars=int(row["total_cost_microdollars"]),
        app_markup_microdollars=int(row.get("app_markup_microdollars") or 0),
        custom_model_markup_microdollars=int(row.get("custom_model_markup_microdollars") or 0),
        usage_type=UsageType.coerce(row["usage_type"]),
        speed_tokens_per_second=float(row["speed_tokens_per_second"]),
        finish_reason=str(row["finish_reason"]),
        status=str(row["status"]),
        streamed=bool(row["streamed"]),
        usage_estimated=bool(row["usage_estimated"]),
        cached_input_tokens=int(row["cached_input_tokens"]),
        reasoning_tokens=int(row["reasoning_tokens"]),
        provider=str(row["provider"] or "") or None,
        elapsed_milliseconds=_optional_int(row.get("elapsed_milliseconds")),
        first_token_milliseconds=_optional_int(row.get("first_token_milliseconds")),
        ttfb_milliseconds=_optional_int(row.get("ttfb_milliseconds")),
        region=str(row["region"]) if row.get("region") is not None else None,
        user=str(row["user"]) if row.get("user") is not None else None,
        session_id=(
            str(row["session_id"]) if row.get("session_id") is not None else None
        ),
        http_referer=(
            str(row["http_referer"])
            if row.get("http_referer") is not None
            else None
        ),
        app_categories=[str(item) for item in row.get("app_categories") or []],
        tags={str(key): str(value) for key, value in (row.get("tags") or {}).items()},
        created_at=_iso(row["created_at"]),
    )


def _usage_bucket_rows(rows: list[dict[str, Any]], *, by_model: bool) -> list[dict[str, Any]]:
    """Fold each bucket's stored usage types into its Credits and BYOK spend.

    Keys follow the usage bucket the stores return. ClickHouse may quote
    64-bit integers in JSON, so every count goes through int().
    """
    merged: dict[tuple[str, str | None], dict[str, Any]] = {}
    for row in rows:
        bucket = str(row["bucket"])
        model = str(row["model"]) if by_model else None
        totals = merged.get((bucket, model))
        if totals is None:
            totals = {"bucket": bucket}
            if model is not None:
                totals["model"] = model
            totals.update(
                requests=0,
                prompt_tokens=0,
                completion_tokens=0,
                reasoning_tokens=0,
                cost_micro=0,
                byok_micro=0,
            )
            merged[(bucket, model)] = totals
        totals["requests"] += int(row["requests"])
        totals["prompt_tokens"] += int(row["prompt_tokens"])
        totals["completion_tokens"] += int(row["completion_tokens"])
        totals["reasoning_tokens"] += int(row["reasoning_token_total"])
        # The same decision generation_metrics makes for one generation.
        spend = "byok_micro" if UsageType.coerce(str(row["usage_type"])).is_byok() else "cost_micro"
        totals[spend] += int(row["cost_total"])
    return list(merged.values())


def settle_elapsed_median(
    rows: list[dict[str, Any]],
) -> dict[str, int | None] | tuple[int, int]:
    """Read the median off one group's elapsed-time histogram, or say which slots to open.

    ``rows`` are the ``slot``, ``elapsed_value`` and ``generations_at_value``
    of ``elapsed_histogram_columns`` for one group, in any order. Each is a
    count of generations: untimed ones (no slot), a whole slot (no value), or
    one distinct value inside an opened slot.

    Returns ``{"generations", "samples", "median_ms"}`` when the median is
    settled, otherwise the two slots the middle values are in, to open in the
    next read. The median is the one a sorted list gives: the middle value
    for an odd count of timed rows, the floor of the mean of the two middle
    values for an even count, ``None`` when no row is timed. It is also
    ``None`` when a middle value is in the open-ended last slot (about 18.6
    hours and up): that slot is never opened, so such a median is reported as
    absent rather than approximated.
    """
    untimed = 0
    groups: list[tuple[int, int, int | None]] = []
    for row in rows:
        count = int(row["generations_at_value"])
        if row.get("slot") is None:
            untimed += count
            continue
        groups.append((int(row["slot"]), count, _optional_int(row.get("elapsed_value"))))
    timed = sum(count for _slot, count, _value in groups)
    summary: dict[str, int | None] = {
        "generations": timed + untimed,
        "samples": timed,
        "median_ms": None,
    }
    if timed == 0:
        return summary
    # Ascending by value: an opened slot's values are all inside that slot.
    groups.sort(key=lambda group: (group[0], -1 if group[2] is None else group[2]))
    # 1-based ranks of the lower and upper middle. They are equal for an odd count.
    ranks = ((timed + 1) // 2, timed // 2 + 1)
    middle: list[tuple[int, int | None]] = []
    position = 0
    through = groups[0][1]
    for rank in ranks:
        while through < rank:
            position += 1
            through += groups[position][1]
        middle.append((groups[position][0], groups[position][2]))
    (low_slot, low), (high_slot, high) = middle
    if high_slot >= ELAPSED_LAST_SLOT:
        # Beyond the histogram: report no median rather than a wrong one.
        return summary
    if low is None or high is None:
        return low_slot, high_slot
    summary["median_ms"] = (low + high) // 2
    return summary


def _client_event(row: dict[str, Any]) -> dict[str, Any]:
    status = _optional_int(row.get("final_http_status"))
    return {
        "created_at": _iso(row.get("created_at")),
        "endpoint": str(row.get("endpoint") or ""),
        "model": str(row.get("model") or "") or None,
        "attempt_host": [str(item) for item in row.get("attempt_host") or []],
        "attempt_count": int(row.get("attempt_count") or 0),
        "final_outcome": str(row.get("final_outcome") or ""),
        "final_http_status": status if status else None,
        "first_error_class": str(row.get("first_error_class") or "") or None,
        "sdk": str(row.get("sdk") or ""),
        "sdk_version": str(row.get("sdk_version") or ""),
        "attempt_request_id": [
            str(item) for item in row.get("attempt_request_id") or [] if item
        ],
    }


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


_TAG_GROUP_KINDS = frozenset({"value", "other", "untagged"})
# Whether a stored usage type is BYOK, as SQL: what ``UsageType.coerce``
# decides, which is ``str(value).strip().lower() == "byok"``. The class of
# blanks is the characters ``str.strip`` removes (``\pZ`` and the control
# characters Python counts as whitespace), and the letters are every
# character ``str.lower`` turns into b, y, o and k, the Kelvin sign among
# them. Anything else is Credits, so a statement that uses this has two cost
# classes whatever spellings are stored. A fixed text: nothing from a request
# is in it.
_TAG_BLANKS = r"[\\pZ\\t\\n\\x0b\\x0c\\r\\x1c-\\x1f\\x{85}]*"
_TAG_IS_BYOK = (
    "match(usage_type, '^" + _TAG_BLANKS + r"[bB][yY][oO][kK\\x{212A}]" + _TAG_BLANKS + "$')"
)
# The tag breakdown's histograms stop at this many slots of ELAPSED_SLOT_MS:
# about 70 minutes. See ``activity_tag_histogram``.
TAG_ELAPSED_SLOTS = 4_096
TAG_ELAPSED_CEILING_MS = TAG_ELAPSED_SLOTS * ELAPSED_SLOT_MS
# The latency the tag histogram counts: an elapsed time at or past the tag
# ceiling is moved to the shared histogram's own ceiling, which puts it in
# the open-ended last slot. Built from constants only.
_TAG_ELAPSED_COLUMN = (
    f"if(elapsed_milliseconds >= {TAG_ELAPSED_CEILING_MS}, {ELAPSED_CEILING_MS}, elapsed_milliseconds)"
)


def _tag_window(
    tenant_id: str, start_at: str, tag_key: str | None, tag_value: str | None
) -> tuple[str, dict[str, str | int]]:
    """The WHERE clause every tag read shares, with its bound values."""
    clauses = [
        "tenant_id = {tenant_id:String}",
        "created_at >= parseDateTime64BestEffort({start_at:String}, 3)",
    ]
    params: dict[str, str | int] = {"tenant_id": tenant_id, "start_at": start_at}
    if tag_key is not None:
        clauses.append("mapContains(tags, {tag_key:String})")
        params["tag_key"] = tag_key
        if tag_value is not None:
            clauses.append("tags[{tag_key:String}] = {tag_value:String}")
            params["tag_value"] = tag_value
    return " AND ".join(clauses), params


# Array parameters are written from integers and the three fixed group kinds
# only. No customer text is ever placed in one, so nothing needs escaping.
def _integer_array(values: list[int]) -> str:
    return "[" + ",".join(str(int(value)) for value in values) + "]"


def _tuple_array(pairs: list[tuple[int, int]]) -> str:
    return "[" + ",".join(f"({int(first)},{int(second)})" for first, second in pairs) + "]"


def _cell_array(cells: list[tuple[str, int, int, int]]) -> str:
    parts = []
    for kind, hash_a, hash_b, slot in cells:
        if kind not in _TAG_GROUP_KINDS:
            raise ValueError("unknown tag group kind")
        parts.append(f"('{kind}',{int(hash_a)},{int(hash_b)},{int(slot)})")
    return "[" + ",".join(parts) + "]"


def _tag_histogram_row(row: dict[str, Any]) -> dict[str, Any]:
    kind = str(row["kind"])
    if kind not in _TAG_GROUP_KINDS:
        raise RuntimeError("ClickHouse tag histogram returned an unknown row kind")
    # -1 stands for NULL inside the packed array: an untimed generation has
    # no slot, and only a value inside an opened slot is kept.
    histogram = [
        (None if int(slot) < 0 else int(slot), None if int(value) < 0 else int(value), int(count))
        for slot, value, count in row.pop("histogram")
    ]
    return {
        "kind": kind,
        "hash_a": int(row["hash_a"]),
        "hash_b": int(row["hash_b"]),
        "requests": int(row["requests"]),
        "cost_microdollars": int(row["cost_microdollars"]),
        "byok_microdollars": int(row["byok_microdollars"]),
        "prompt_tokens": int(row["prompt_tokens"]),
        "completion_tokens": int(row["completion_tokens"]),
        "histogram": histogram,
    }
