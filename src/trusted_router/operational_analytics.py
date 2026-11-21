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


@contextmanager
def bound_values_stay_private() -> Iterator[None]:
    """Run a read whose bound values must not leave in telemetry or errors.

    ClickHouse takes bound values as URL parameters. Sentry's httpx
    integration copies an outgoing request's query string into a span and a
    breadcrumb, and httpx names the whole URL in an ``HTTPStatusError``. Tag
    keys and values are customer text that must never reach a log or Sentry
    (docs/design/request-tagging.md, section 10). So a tag read runs under a
    scope whose client has no integrations and no transport, and a status
    error is raised again without its URL. The price is that these requests
    are absent from traces.
    """
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
        for key, value in (params or {}).items():
            query_params[f"param_{key}"] = str(value)
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
                    lambda endpoint: client.build_request(
                        "POST",
                        endpoint,
                        params=query_params,
                        content=sql,
                        headers={"content-type": "text/plain; charset=utf-8"},
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
    # Each read covers every generation of one tenant since ``start_at``, not
    # a sample, and returns a bounded number of groups. Tag keys and values
    # are always bound; ``bound_values_stay_private`` keeps them out of Sentry
    # and out of error text.

    def activity_tag_keys(
        self, *, tenant_id: str, start_at: str, limit: int
    ) -> list[dict[str, Any]]:
        """Tag keys by the number of generations carrying them."""
        with bound_values_stay_private():
            rows = self._query(
                """
SELECT tag_key, count() AS requests
FROM
(
  SELECT arrayJoin(mapKeys(tags)) AS tag_key
  FROM activity_generations FINAL
  WHERE tenant_id = {tenant_id:String}
    AND created_at >= parseDateTime64BestEffort({start_at:String}, 3)
)
GROUP BY tag_key
ORDER BY requests DESC, tag_key ASC
LIMIT {limit:UInt32}
FORMAT JSON
""",
                params={"tenant_id": tenant_id, "start_at": start_at, "limit": max(1, limit)},
            )
        return [{"key": str(row["tag_key"]), "requests": int(row["requests"])} for row in rows]

    def activity_tag_values(
        self, *, tenant_id: str, tag_key: str, start_at: str, limit: int
    ) -> list[dict[str, Any]]:
        """One key's values by the number of generations carrying them."""
        with bound_values_stay_private():
            rows = self._query(
                """
SELECT tags[{tag_key:String}] AS tag_value, count() AS requests
FROM activity_generations FINAL
WHERE tenant_id = {tenant_id:String}
  AND created_at >= parseDateTime64BestEffort({start_at:String}, 3)
  AND mapContains(tags, {tag_key:String})
GROUP BY tag_value
ORDER BY requests DESC, tag_value ASC
LIMIT {limit:UInt32}
FORMAT JSON
""",
                params={
                    "tenant_id": tenant_id,
                    "tag_key": tag_key,
                    "start_at": start_at,
                    "limit": max(1, limit),
                },
            )
        return [{"value": str(row["tag_value"]), "requests": int(row["requests"])} for row in rows]

    def activity_tag_breakdown(
        self,
        *,
        tenant_id: str,
        group_by: str,
        start_at: str,
        limit: int,
        tag_key: str | None = None,
        tag_value: str | None = None,
    ) -> list[dict[str, Any]]:
        """Totals per value of ``group_by``, at most ``limit`` + 2 rows.

        The ``limit`` values with the highest credit cost keep a row each, the
        rest are summed into ``other`` and generations without the key into
        ``untagged``. The ranking is an uncorrelated subquery, so ClickHouse
        runs it once. ``tag_key``/``tag_value`` narrow both passes to one tag.
        The median is exact: ``quantileExactWeighted`` keeps one counter per
        distinct latency, where ``quantileExact`` would keep every row's.
        """
        clauses = [
            "tenant_id = {tenant_id:String}",
            "created_at >= parseDateTime64BestEffort({start_at:String}, 3)",
        ]
        params: dict[str, str | int] = {
            "tenant_id": tenant_id,
            "group_by": group_by,
            "start_at": start_at,
            "limit": max(1, limit),
        }
        if tag_key is not None:
            clauses.append("mapContains(tags, {tag_key:String})")
            params["tag_key"] = tag_key
            if tag_value is not None:
                clauses.append("tags[{tag_key:String}] = {tag_value:String}")
                params["tag_value"] = tag_value
        where = " AND ".join(clauses)
        with bound_values_stay_private():
            rows = self._query(
                """
SELECT
  multiIf(
    NOT mapContains(tags, {group_by:String}), 'untagged',
    tags[{group_by:String}] IN
    (
      SELECT tags[{group_by:String}] AS top_value
      FROM activity_generations FINAL
      WHERE """
                + where
                + """
        AND mapContains(tags, {group_by:String})
      GROUP BY top_value
      ORDER BY
        sumIf(total_cost_microdollars, lower(trimBoth(usage_type)) != 'byok') DESC,
        count() DESC,
        top_value ASC
      LIMIT {limit:UInt32}
    ), 'value',
    'other'
  ) AS kind,
  if(kind = 'value', tags[{group_by:String}], '') AS group_value,
  uniqExactIf(tags[{group_by:String}], mapContains(tags, {group_by:String})) AS value_count,
  count() AS requests,
  sumIf(total_cost_microdollars, lower(trimBoth(usage_type)) != 'byok') AS cost_microdollars,
  sumIf(total_cost_microdollars, lower(trimBoth(usage_type)) = 'byok') AS byok_microdollars,
  sum(tokens_prompt) AS prompt_tokens,
  sum(tokens_completion) AS completion_tokens,
  quantileExactWeighted(0.5)(elapsed_milliseconds, 1) AS median_elapsed_ms
FROM activity_generations FINAL
WHERE """
                + where
                + """
GROUP BY kind, group_value
FORMAT JSON
""",
                params=params,
            )
        return [_tag_breakdown_row(row) for row in rows]

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


def _tag_breakdown_row(row: dict[str, Any]) -> dict[str, Any]:
    kind = str(row["kind"])
    if kind not in {"value", "other", "untagged"}:
        raise RuntimeError("ClickHouse tag breakdown returned an unknown row kind")
    return {
        "kind": kind,
        "value": str(row["group_value"]) if kind == "value" else None,
        "values": int(row["value_count"]),
        "requests": int(row["requests"]),
        "cost_microdollars": int(row["cost_microdollars"]),
        "byok_microdollars": int(row["byok_microdollars"]),
        "prompt_tokens": int(row["prompt_tokens"]),
        "completion_tokens": int(row["completion_tokens"]),
        "median_elapsed_ms": _optional_int(row.get("median_elapsed_ms")),
    }
