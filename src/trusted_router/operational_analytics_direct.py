"""Direct-to-ClickHouse sink for operational analytics.

WHY THIS EXISTS. Operational telemetry -- activity rows, synthetic probe
samples, client SDK beacons -- used to be written into a 32-shard outbox table
in the BILLING database and polled out by a drainer at 64 queries per cycle,
every ~1.5 seconds, forever. Spanner's own query stats measured that drain at
~25% of the whole instance's CPU while doing nothing, and on launch day
(2026-08-25) that standing tax was the missing headroom that let a modest
traffic bump collapse authorize/settle for every cloud. The outbox pattern
buys transactional coupling with business writes -- a property operational
telemetry does not need and should not pay for. Bounded, counted loss is the
correct contract for ops data; the money path is for money.

So this sink writes telemetry straight to ClickHouse: canonicalise at the
producer, buffer in memory (bounded, drop-oldest, counted), flush in batches
from one background thread. Loss window on an instance kill is one flush
interval of TELEMETRY, not revenue. Selected per deployment via
``settings.operational_analytics_sink`` ("outbox" stays the default until a
cloud's ClickHouse write credential is provisioned; see config validation).

The canonicalisation below is EXTRACTED VERBATIM from
clickhouse/ingest_operational_outbox.py so rows are byte-identical to what the
drainer produced. The drainer keeps a frozen copy only until the outbox is
decommissioned; clickhouse/ingest_operational_outbox_postgres.py (AWS/Azure)
keeps its own until those planes cut over. Do not let the copies drift --
delete them.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from trusted_router.storage_models import Generation, SyntheticProbeSample
from trusted_router.storage_operational_analytics import (
    ACTIVITY_EVENT_KIND,
    CLIENT_EVENTS_EVENT_KIND,
    SYNTHETIC_EVENT_KIND,
    activity_payload,
    synthetic_payload,
)

log = logging.getLogger("trusted_router.operational_analytics_direct")

ACTIVITY_COLUMNS = (
    "generation_id",
    "request_id",
    "tenant_id",
    "workspace_id",
    "key_id",
    "model",
    "provider",
    "provider_name",
    "app",
    "tokens_prompt",
    "tokens_completion",
    "cached_input_tokens",
    "reasoning_tokens",
    "total_cost_microdollars",
    "usage_type",
    "speed_tokens_per_second",
    "finish_reason",
    "status",
    "streamed",
    "usage_estimated",
    "elapsed_milliseconds",
    "first_token_milliseconds",
    "ttfb_milliseconds",
    "region",
    "user",
    "session_id",
    "http_referer",
    "app_categories",
    "tags",
    "created_at",
    "gateway_request_id",
    "synthetic",
    "client_source",
    "client_sdk",
    "client_sdk_version",
    "client_lang",
    "client_runtime",
    "client_os",
    "client_arch",
    "client_timeout_ms",
    "client_attempt",
    "client_prev_outcome",
    "client_prev_error_class",
    "client_prev_host",
    "client_prev_elapsed_ms",
    "client_since_first_ms",
    "client_stream",
    "client_failover_used",
)
ACTIVITY_OPTIONAL_DEFAULTS: dict[str, Any] = {
    "gateway_request_id": "",
    "synthetic": 0,
    "client_source": "none",
    "client_sdk": "",
    "client_sdk_version": "",
    "client_lang": "",
    "client_runtime": "",
    "client_os": "",
    "client_arch": "",
    "client_timeout_ms": None,
    "client_attempt": None,
    "client_prev_outcome": "",
    "client_prev_error_class": "",
    "client_prev_host": "",
    "client_prev_elapsed_ms": None,
    "client_since_first_ms": None,
    "client_stream": None,
    "client_failover_used": None,
}
ACTIVITY_BOOLEAN_COLUMNS = (
    "streamed",
    "usage_estimated",
    "synthetic",
    "client_stream",
    "client_failover_used",
)
SYNTHETIC_COLUMNS = (
    "id",
    "probe_type",
    "target",
    "target_url",
    "monitor_region",
    "status",
    "target_region",
    "latency_milliseconds",
    "ttfb_milliseconds",
    "dns_milliseconds",
    "tcp_connect_milliseconds",
    "tls_handshake_milliseconds",
    "gateway_processing_milliseconds",
    "connection_reused",
    "protocol",
    "http_status",
    "error_type",
    "provider",
    "model",
    "selected_provider",
    "selected_model",
    "generation_id",
    "attestation_digest",
    "source_commit",
    "cost_microdollars",
    "output_match",
    "created_at",
)

CLIENT_REQUEST_COLUMNS = (
    "event_id",
    "tenant_id",
    "key_id",
    "batch_id",
    "instance_id",
    "seq",
    "received_at",
    "created_at",
    "clock_skew_ms",
    "synthetic",
    "sdk",
    "sdk_version",
    "lang",
    "runtime",
    "os",
    "arch",
    "plane",
    "endpoint",
    "method",
    "streaming",
    "provider_pinned",
    "model",
    "final_outcome",
    "final_http_status",
    "final_host",
    "first_error_class",
    "error_source",
    "total_ms",
    "ttft_ms",
    "timeout_phase",
    "configured_timeout_ms",
    "attempt_count",
    "failover_used",
    "attempt_host",
    "attempt_outcome",
    "attempt_http_status",
    "attempt_error_class",
    "attempt_error_source",
    "attempt_should_retry",
    "attempt_retry_after_ms",
    "attempt_elapsed_ms",
    "attempt_ttfb_ms",
    "attempt_request_id",
    "attempt_moved",
    "sample_rate",
    "sample_reason",
    "tr_fault",
    "methodology_version",
)
CLIENT_COUNTER_COLUMNS = (
    "event_id",
    "tenant_id",
    "key_id",
    "instance_id",
    "bucket_start",
    "received_at",
    "synthetic",
    "sdk",
    "sdk_version",
    "level",
    "endpoint",
    "streaming",
    "host",
    "outcome",
    "error_class",
    "http_status_class",
    "timeout_phase",
    "timeout_floor_met",
    "provider_pinned",
    "requests",
    "attempts",
    "failover_used",
    "first_attempt_success",
    "total_ms_hist",
    "first_event_ms_hist",
    "tr_fault",
    "methodology_version",
)

EVENT_TABLES = {
    "activity": "activity_generations",
    "synthetic": "synthetic_probe_samples",
    "client_events": ("client_request_events", "client_minute_counters"),
    "client_request": "client_request_events",
    "client_counter": "client_minute_counters",
    "quarantine": "operational_outbox_quarantine",
}


@dataclass(frozen=True)
class OperationalOutboxRow:
    shard: int
    commit_ts: dt.datetime
    event_kind: str
    event_id: str
    payload: str

    @property
    def key(self) -> tuple[int, dt.datetime, str, str]:
        return (self.shard, self.commit_ts, self.event_kind, self.event_id)


@dataclass(frozen=True)
class CanonicalOperationalEvent:
    event_kind: str
    row: dict[str, Any]


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _event_id(tenant_id: str, batch_id: str, kind: str, index: int) -> str:
    value = f"{tenant_id}:{batch_id}:{kind}:{index}"
    return hashlib.sha256(value.encode()).hexdigest()


def _object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"client_events {field} is not a JSON object")
    return value


def _objects(value: Any, *, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"client_events {field} is not a JSON array")
    return [_object(item, field=f"{field}[{index}]") for index, item in enumerate(value)]


def _required(payload: dict[str, Any], field: str) -> Any:
    try:
        return payload[field]
    except KeyError:
        raise ValueError(f"client_events payload missing required field: {field}") from None


def _retry_value(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value == "true" or value == "false":
        return str(value)
    return "absent"


def expand_client_events_payload(
    payload: dict[str, Any],
    commit_ts: dt.datetime,
) -> list[CanonicalOperationalEvent]:
    """Expand one validated beacon POST into ClickHouse request/counter rows."""

    if _required(payload, "schema_version") != 1:
        raise ValueError("client_events schema_version must be 1")
    tenant_id = str(_required(payload, "tenant_id"))
    key_id = str(_required(payload, "key_id"))
    batch_id = str(_required(payload, "batch_id"))
    instance_id = str(_required(payload, "instance_id"))
    received_at = _required(payload, "received_at")
    clock_skew_ms = int(_required(payload, "clock_skew_ms"))
    synthetic = int(bool(_required(payload, "synthetic")))
    seq = int(_required(payload, "seq"))
    sdk = _object(_required(payload, "sdk"), field="sdk")
    sdk_name = str(_required(sdk, "name"))
    sdk_version = str(_required(sdk, "version"))
    ingest_version = _utc(commit_ts).isoformat()
    common = {
        "tenant_id": tenant_id,
        "key_id": key_id,
        "instance_id": instance_id,
        "received_at": received_at,
        "synthetic": synthetic,
        "sdk": sdk_name,
        "sdk_version": sdk_version,
    }
    result: list[CanonicalOperationalEvent] = []
    for index, event in enumerate(_objects(_required(payload, "events"), field="events")):
        attempts = _objects(_required(event, "attempts"), field=f"events[{index}].attempts")
        if not attempts:
            raise ValueError(f"client_events events[{index}].attempts is empty")
        error_classes = [
            "" if attempt.get("error_class") is None else str(attempt["error_class"])
            for attempt in attempts
        ]
        error_sources = [
            "" if attempt.get("error_source") is None else str(attempt["error_source"])
            for attempt in attempts
        ]
        row = {
            "event_id": _event_id(tenant_id, batch_id, "r", index),
            **common,
            "batch_id": batch_id,
            "seq": seq,
            "created_at": _required(event, "created_at"),
            "clock_skew_ms": clock_skew_ms,
            "lang": str(_required(sdk, "lang")),
            "runtime": str(_required(sdk, "runtime")),
            "os": str(_required(sdk, "os")),
            "arch": str(_required(sdk, "arch")),
            "plane": _required(event, "plane"),
            "endpoint": _required(event, "endpoint"),
            "method": _required(event, "method"),
            "streaming": int(bool(_required(event, "streaming"))),
            "provider_pinned": int(bool(_required(event, "provider_pinned"))),
            "model": "" if event.get("model") is None else str(event["model"]),
            "final_outcome": _required(event, "final_outcome"),
            "final_http_status": int(event.get("final_http_status") or 0),
            "final_host": str(_required(attempts[-1], "host")),
            "first_error_class": next((value for value in error_classes if value), ""),
            "error_source": next((value for value in error_sources if value), ""),
            "total_ms": int(_required(event, "total_ms")),
            "ttft_ms": event.get("ttft_ms"),
            "timeout_phase": _required(event, "timeout_phase"),
            "configured_timeout_ms": event.get("configured_timeout_ms"),
            "attempt_count": len(attempts),
            "failover_used": int(bool(_required(event, "failover_used"))),
            "attempt_host": [str(_required(attempt, "host")) for attempt in attempts],
            "attempt_outcome": [str(_required(attempt, "outcome")) for attempt in attempts],
            "attempt_http_status": [int(attempt.get("http_status") or 0) for attempt in attempts],
            "attempt_error_class": error_classes,
            "attempt_error_source": error_sources,
            "attempt_should_retry": [
                _retry_value(attempt.get("should_retry")) for attempt in attempts
            ],
            "attempt_retry_after_ms": [
                int(attempt.get("retry_after_ms") or 0) for attempt in attempts
            ],
            "attempt_elapsed_ms": [int(_required(attempt, "elapsed_ms")) for attempt in attempts],
            "attempt_ttfb_ms": [int(attempt.get("ttfb_ms") or 0) for attempt in attempts],
            "attempt_request_id": [
                "" if attempt.get("request_id") is None else str(attempt["request_id"])
                for attempt in attempts
            ],
            "attempt_moved": [int(bool(_required(attempt, "moved"))) for attempt in attempts],
            "sample_rate": float(_required(event, "sample_rate")),
            "sample_reason": _required(event, "sample_reason"),
            "tr_fault": int(bool(_required(event, "tr_fault"))),
            "methodology_version": int(_required(event, "methodology_version")),
            "ingest_version": ingest_version,
        }
        result.append(CanonicalOperationalEvent(event_kind="client_request", row=row))
    for index, counter in enumerate(_objects(_required(payload, "counters"), field="counters")):
        row = {
            "event_id": _event_id(tenant_id, batch_id, "c", index),
            **common,
            "bucket_start": _required(counter, "bucket_start"),
            "level": _required(counter, "level"),
            "endpoint": _required(counter, "endpoint"),
            "streaming": int(bool(_required(counter, "streaming"))),
            "host": _required(counter, "host"),
            "outcome": _required(counter, "outcome"),
            "error_class": ("" if counter.get("error_class") is None else counter["error_class"]),
            "http_status_class": _required(counter, "http_status_class"),
            "timeout_phase": _required(counter, "timeout_phase"),
            "timeout_floor_met": int(bool(_required(counter, "timeout_floor_met"))),
            "provider_pinned": int(bool(_required(counter, "provider_pinned"))),
            "requests": int(_required(counter, "requests")),
            "attempts": int(_required(counter, "attempts")),
            "failover_used": int(_required(counter, "failover_used")),
            "first_attempt_success": int(_required(counter, "first_attempt_success")),
            "total_ms_hist": _object(
                _required(counter, "total_ms_hist"),
                field=f"counters[{index}].total_ms_hist",
            ),
            "first_event_ms_hist": _object(
                _required(counter, "first_event_ms_hist"),
                field=f"counters[{index}].first_event_ms_hist",
            ),
            "tr_fault": int(bool(_required(counter, "tr_fault"))),
            "methodology_version": int(_required(counter, "methodology_version")),
            "ingest_version": ingest_version,
        }
        result.append(CanonicalOperationalEvent(event_kind="client_counter", row=row))
    if not result:
        raise ValueError("client_events payload has no events or counters")
    return result


def normalise_operational_event(
    row: OperationalOutboxRow,
) -> list[CanonicalOperationalEvent]:
    raw = json.loads(row.payload)
    if not isinstance(raw, dict):
        raise ValueError("operational outbox payload is not a JSON object")
    if row.event_kind == "client_events":
        try:
            return expand_client_events_payload(raw, row.commit_ts)
        except (TypeError, ValueError) as exc:
            raise ValueError(str(exc)) from None
    allowed: tuple[str, ...]
    required: tuple[str, ...]
    if row.event_kind == "activity":
        allowed = ACTIVITY_COLUMNS
        required = tuple(
            column for column in ACTIVITY_COLUMNS if column not in ACTIVITY_OPTIONAL_DEFAULTS
        )
    elif row.event_kind == "synthetic":
        allowed = SYNTHETIC_COLUMNS
        required = SYNTHETIC_COLUMNS
    else:
        raise ValueError(f"unsupported operational event kind: {row.event_kind}")
    missing = [column for column in required if column not in raw]
    if missing:
        raise ValueError(f"{row.event_kind} payload missing required fields: {', '.join(missing)}")
    canonical: dict[str, Any] = {}
    for column in allowed:
        value = raw.get(column)
        if row.event_kind == "activity" and value is None:
            default = ACTIVITY_OPTIONAL_DEFAULTS.get(column)
            if default is not None:
                value = default
        if row.event_kind == "activity" and column in ACTIVITY_BOOLEAN_COLUMNS:
            if value is not None:
                value = int(bool(value))
        canonical[column] = value
    canonical["ingest_version"] = _utc(row.commit_ts).isoformat()
    return [CanonicalOperationalEvent(event_kind=row.event_kind, row=canonical)]


def quarantine_event(
    row: OperationalOutboxRow,
    exc: ValueError,
    *,
    now: dt.datetime | None = None,
) -> CanonicalOperationalEvent:
    return CanonicalOperationalEvent(
        event_kind="quarantine",
        row={
            "shard": row.shard,
            "commit_ts": _utc(row.commit_ts).isoformat(),
            "event_kind": row.event_kind,
            "event_id": row.event_id,
            "payload": row.payload,
            "reason": str(exc)[:500],
            "quarantined_at": _utc(now or dt.datetime.now(dt.UTC)).isoformat(),
        },
    )


# ---------------------------------------------------------------------------
# The sink
# ---------------------------------------------------------------------------

#: Bounded buffer size. At ~2 rows/second of steady telemetry this is hours of
#: ClickHouse outage before anything drops; when it does drop it drops OLDEST
#: (the newest telemetry is the operationally interesting end) and counts.
DEFAULT_BUFFER_ROWS = 20_000
DEFAULT_FLUSH_INTERVAL_SECONDS = 2.0
DEFAULT_FLUSH_BATCH_ROWS = 1_000
#: Failure backoff cap. Telemetry freshness is worth little enough that
#: hammering a down ClickHouse would be its own kind of self-harm.
FAILURE_BACKOFF_CAP_SECONDS = 30.0


@dataclass
class SinkStats:
    published: int = 0
    inserted: int = 0
    dropped: int = 0
    quarantined: int = 0
    flush_failures: int = 0
    last_success_unix: float = 0.0


class DirectOperationalAnalyticsSink:
    """Bounded in-process buffer draining to ClickHouse from one thread.

    Duck-type-compatible with SpannerOperationalAnalyticsOutbox /
    PostgresOperationalAnalyticsOutbox where the stores touch it: the
    ``enqueue_*`` family and ``oldest_enqueued_at``. The ``_tx`` variants
    accept and ignore the transaction handle -- the whole point is that
    telemetry no longer rides business transactions. A transaction that
    retries may therefore publish the same event twice, and an aborted one
    may publish a phantom; ClickHouse deduplicates by event_id and the data
    is operational, so both are accepted trades, stated here on purpose.
    """

    def __init__(
        self,
        *,
        url: str,
        database: str,
        user: str,
        password: str,
        buffer_rows: int = DEFAULT_BUFFER_ROWS,
        flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        flush_batch_rows: int = DEFAULT_FLUSH_BATCH_ROWS,
        post: Callable[[str, bytes, dict[str, str]], None] | None = None,
        start_thread: bool = True,
    ) -> None:
        if not url:
            raise ValueError("direct operational analytics sink requires a ClickHouse url")
        if not password:
            raise ValueError(
                "direct operational analytics sink requires a WRITE-capable "
                "ClickHouse credential; the read user cannot INSERT"
            )
        self._url = url.rstrip("/")
        self._database = database
        # .strip() both halves. `openssl rand -hex 24 | gcloud secrets create
        # --data-file=-` stores a TRAILING NEWLINE, and every provisioning
        # script that reads the value back through command substitution
        # silently drops it -- so the hash installed on the ClickHouse user
        # and the password injected into this process differ by one byte.
        # That is exactly how the 2026-08-26 GCP cutover went live "verified"
        # and delivered nothing: 401 on every flush. Trailing whitespace in a
        # credential is never meaningful; refusing to be broken by it is free.
        self._auth = base64.b64encode(f"{user.strip()}:{password.strip()}".encode()).decode()
        self._buffer_rows = buffer_rows
        self._buffer: deque[tuple[str, dict[str, Any], dt.datetime]] = deque(maxlen=buffer_rows)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._flush_interval = flush_interval_seconds
        self._flush_batch = flush_batch_rows
        self._post = post or self._http_post
        self.stats = SinkStats()
        self._thread: threading.Thread | None = None
        if start_thread:
            self._thread = threading.Thread(
                target=self._run, name="operational-analytics-direct", daemon=True
            )
            self._thread.start()

    # -- producer surface (documented duck-type of the outbox writers) ------

    def enqueue_activity(self, generation: Generation) -> None:
        self._publish(ACTIVITY_EVENT_KIND, generation.id, activity_payload(generation))

    def enqueue_activity_tx(self, transaction: Any, generation: Generation) -> None:
        self.enqueue_activity(generation)

    def enqueue_synthetic(self, sample: SyntheticProbeSample) -> None:
        self._publish(SYNTHETIC_EVENT_KIND, sample.id, synthetic_payload(sample))

    def enqueue_synthetic_tx(self, transaction: Any, sample: SyntheticProbeSample) -> None:
        self.enqueue_synthetic(sample)

    def enqueue_client_events(self, payload: dict[str, Any]) -> None:
        self._publish(
            CLIENT_EVENTS_EVENT_KIND,
            f"{payload['tenant_id']}:{payload['batch_id']}",
            payload,
        )

    def oldest_enqueued_at(self, *, timeout: float | None = None) -> dt.datetime | None:
        """Age of the oldest UNDELIVERED row, or None when the buffer is empty.

        This answers the same question the durable outboxes answer, which is
        the entire point: /status.json's `analytics.drain_lag_seconds` and the
        freshness gate already know how to alarm on a growing backlog, so a
        sink that cannot deliver must grow a number those consumers can see.

        Returning a flat None here -- as this method first did -- is how the
        2026-08-26 cutover failed SILENTLY: every flush 401'd, telemetry was
        dropped by the bounded buffer, and the only outward sign was synthetic
        samples quietly ceasing to arrive. A delivery path whose failure mode
        is invisible is not an improvement over the one it replaced.
        """
        oldest, _stats = self.freshness_snapshot()
        return oldest

    def freshness_snapshot(self) -> tuple[dt.datetime | None, SinkStats]:
        """Return one lock-consistent delivery-health snapshot.

        The public freshness surface needs the buffer head and cumulative
        counters from the same instant.  Returning a copy also prevents a
        caller from observing the mutable ``stats`` object while the flusher
        changes it underneath serialization.
        """
        with self._lock:
            oldest = (
                min(commit_ts for _table, _row, commit_ts in self._buffer)
                if self._buffer
                else None
            )
            stats = SinkStats(
                published=self.stats.published,
                inserted=self.stats.inserted,
                dropped=self.stats.dropped,
                quarantined=self.stats.quarantined,
                flush_failures=self.stats.flush_failures,
                last_success_unix=self.stats.last_success_unix,
            )
        return oldest, stats

    # -- internals ----------------------------------------------------------

    def _publish(self, event_kind: str, event_id: str, payload: dict[str, Any]) -> None:
        row = OperationalOutboxRow(
            shard=0,
            commit_ts=dt.datetime.now(dt.UTC),
            event_kind=event_kind,
            event_id=event_id,
            payload=json.dumps(payload),
        )
        try:
            events = normalise_operational_event(row)
            quarantined = False
        except ValueError as exc:
            events = [quarantine_event(row, exc)]
            quarantined = True
        with self._lock:
            before = len(self._buffer)
            for event in events:
                table = EVENT_TABLES[event.event_kind]
                assert isinstance(table, str)
                self._buffer.append((table, event.row, row.commit_ts))
            overflow = before + len(events) - self._buffer_rows
            if overflow > 0:
                self.stats.dropped += overflow
            if quarantined:
                self.stats.quarantined += 1
            self.stats.published += len(events)
            buffered = len(self._buffer)
        if buffered >= self._flush_batch:
            self._wake.set()

    def _run(self) -> None:
        failure_delay = self._flush_interval
        while not self._closed.is_set():
            self._wake.wait(timeout=self._flush_interval)
            self._wake.clear()
            try:
                self.flush()
            except Exception as exc:
                _oldest, stats = self.freshness_snapshot()
                with self._lock:
                    buffered = len(self._buffer)
                # error(), not just exception(): this must be greppable and
                # must carry the counts, because the failure is otherwise
                # invisible from outside the process.
                log.error(
                    "operational_analytics_direct.flush_failed "
                    "buffered=%d dropped=%d failures=%d error=%s: %s",
                    buffered,
                    stats.dropped,
                    stats.flush_failures,
                    type(exc).__name__,
                    str(exc)[:200],
                )
                # Failure backoff: nothing was consumed, so retry later
                # rather than immediately -- a down ClickHouse must not turn
                # this thread into a hot loop.
                self._closed.wait(timeout=failure_delay)
                failure_delay = min(failure_delay * 2, FAILURE_BACKOFF_CAP_SECONDS)
                continue
            failure_delay = self._flush_interval

    def flush(self) -> int:
        """Send everything buffered, grouped per table. Raises on failure
        with rows RETAINED (front of the deque) for the next attempt."""
        with self._lock:
            batch = list(self._buffer)
            self._buffer.clear()
        if not batch:
            return 0
        by_table: dict[str, list[tuple[dict[str, Any], dt.datetime]]] = {}
        for table, row, commit_ts in batch:
            by_table.setdefault(table, []).append((row, commit_ts))
        sent = 0
        try:
            for table, rows in by_table.items():
                body = "\n".join(json.dumps(row, default=str) for row, _ts in rows).encode()
                query = f"INSERT INTO {self._database}.{table} FORMAT JSONEachRow"
                self._post(
                    f"{self._url}/?query={urllib.parse.quote(query)}",
                    body,
                    {
                        "Authorization": f"Basic {self._auth}",
                        "Content-Type": "application/x-ndjson",
                    },
                )
                sent += len(rows)
                by_table[table] = []
        except Exception:
            with self._lock:
                # Put back everything not confirmed sent, oldest first, ahead
                # of anything published meanwhile. maxlen drops overflow from
                # the RIGHT via appendleft ordering -- newest retained.
                remaining = [
                    (table, row, commit_ts)
                    for table, rows in by_table.items()
                    for row, commit_ts in rows
                ]
                for item in reversed(remaining):
                    self._buffer.appendleft(item)
                self.stats.flush_failures += 1
            raise
        with self._lock:
            self.stats.inserted += sent
            if sent:
                self.stats.last_success_unix = time.time()
        return sent

    def close(self) -> None:
        self._closed.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            self.flush()
        except Exception:
            log.exception("operational_analytics_direct.final_flush_failed")

    @staticmethod
    def _http_post(url: str, body: bytes, headers: dict[str, str]) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"clickhouse url must be http(s), got {url.split(':', 1)[0]!r}")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            response.read()
