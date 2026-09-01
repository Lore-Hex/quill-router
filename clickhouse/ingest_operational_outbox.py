"""Drain tenant activity and synthetic metadata from Spanner to ClickHouse."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Protocol

from clickhouse._sdnotify import sd_notify

PROJECT = "quill-cloud-proxy"
SPANNER_INSTANCE = "trusted-router-nam6"
SPANNER_DATABASE = "trusted-router"
OUTBOX_TABLE = "tr_operational_analytics_outbox"
OUTBOX_SHARDS = 32

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
SPEND_LEASE_SHADOW_COLUMNS = (
    "event_id",
    "created_at",
    "workspace_id",
    "key_hash",
    "boot_kid",
    "boot_verified",
    "lease_id",
    "no_lease_reason",
    "echo_state",
    "would_admit",
    "enclave_estimate_micro",
    "server_estimate_micro",
    "server_verdict",
    "catalog_version",
    "divergence",
    "schema_version",
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
    "spend_lease_shadow": "spend_lease_shadow",
    "client_events": ("client_request_events", "client_minute_counters"),
    "client_request": "client_request_events",
    "client_counter": "client_minute_counters",
    "quarantine": "operational_outbox_quarantine",
}

log = logging.getLogger("trusted_router.operational_analytics_ingest")


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


@dataclass(frozen=True)
class DrainResult:
    fetched: int
    inserted: int
    rows_per_second: float
    quarantined: int = 0
    lag_seconds: float = 0.0


class OutboxSource(Protocol):
    def fetch(self, *, limit: int) -> list[OperationalOutboxRow]: ...

    def delete(self, rows: list[OperationalOutboxRow]) -> None: ...

    def oldest_commit_ts(self) -> dt.datetime | None: ...


class BatchWriter(Protocol):
    def insert(self, events: list[CanonicalOperationalEvent]) -> None: ...


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


class SpannerOperationalOutboxSource:
    def __init__(
        self,
        *,
        project: str,
        instance: str,
        database: str,
        shard_count: int = OUTBOX_SHARDS,
    ) -> None:
        from google.cloud import spanner
        from google.cloud.spanner_v1 import param_types

        self._database = (
            spanner.Client(project=project, disable_builtin_metrics=True)
            .instance(instance)
            .database(database)
        )
        self._pt = param_types
        self._shard_count = shard_count

    def fetch(self, *, limit: int) -> list[OperationalOutboxRow]:
        if limit < 1:
            return []
        # Delivery order is not load-bearing: ClickHouse replacement uses the
        # stable outbox commit timestamp as its version, and delete addresses
        # only the exact fetched primary keys after every insert succeeds.
        # Quarantine rows are append-only diagnostics and can repeat after a
        # partial insert retry, but they have no ordering semantics.
        # One global LIMIT therefore replaces the ordered query per shard.
        rows: list[OperationalOutboxRow] = []
        with self._database.snapshot() as snapshot:
            values = snapshot.execute_sql(
                # OUTBOX_TABLE is a module constant, not caller input.
                "SELECT shard, commit_ts, event_kind, event_id, payload "  # noqa: S608
                f"FROM {OUTBOX_TABLE} LIMIT @limit",
                params={"limit": limit},
                param_types={"limit": self._pt.INT64},
            )
            rows.extend(
                OperationalOutboxRow(
                    shard=int(row[0]),
                    commit_ts=_utc(row[1]),
                    event_kind=str(row[2]),
                    event_id=str(row[3]),
                    payload=str(row[4]),
                )
                for row in values
            )
        return rows

    def delete(self, rows: list[OperationalOutboxRow]) -> None:
        if not rows:
            return
        from google.cloud.spanner_v1 import KeySet

        with self._database.batch() as batch:
            batch.delete(OUTBOX_TABLE, KeySet(keys=[list(row.key) for row in rows]))

    def oldest_commit_ts(self) -> dt.datetime | None:
        oldest: dt.datetime | None = None
        with self._database.snapshot(multi_use=True) as snapshot:
            for shard in range(self._shard_count):
                values = list(
                    snapshot.execute_sql(
                        "SELECT commit_ts FROM tr_operational_analytics_outbox "
                        "WHERE shard=@shard ORDER BY commit_ts LIMIT 1",
                        params={"shard": shard},
                        param_types={"shard": self._pt.INT64},
                    )
                )
                if values:
                    candidate = _utc(values[0][0])
                    oldest = candidate if oldest is None else min(oldest, candidate)
        return oldest


class ClickHouseInsertError(RuntimeError):
    """A ClickHouse target did not accept a complete logical batch."""


class ClickHouseOperationalWriter:
    """Inserts a batch into one ClickHouse endpoint via clickhouse-client.

    `host`/`port`/`secure` default to unset, and an unset connection flag is
    omitted from argv entirely rather than passed as a default. That is what
    keeps a writer constructed the historical way — password/user/database
    only — emitting the byte-identical command it always did, talking to the
    node on this machine. A drain with one configured endpoint therefore
    behaves exactly as it did before remote endpoints existed.
    """

    def __init__(
        self,
        *,
        password: str,
        database: str = "tr",
        user: str = "tr",
        host: str = "",
        port: int = 0,
        secure: bool = False,
        timeout_seconds: float | None = None,
    ) -> None:
        self._password = password
        self._database = database
        # The user was hardcoded to "tr" while the database was already a
        # parameter, so this class silently only worked on the GCP cluster.
        # The AWS-EU node authenticates as "default" into database "default"
        # (its schema is applied unqualified), so a drain pointed at it failed
        # authentication on the very first insert -- after the rows had been
        # read, which is the worst place to discover a credential mismatch.
        self._user = user
        self._host = host
        self._port = port
        self._secure = secure
        # None means "wait forever", which is what the local writer has always
        # done and what it keeps doing. A REMOTE endpoint needs a finite bound:
        # a node that completes the TCP handshake and then stalls -- a wedged
        # server, a silently blackholed path -- would otherwise hang
        # subprocess.run indefinitely, freezing the whole sweep inside a daemon
        # that still looks alive while the outbox grows behind it.
        self._timeout_seconds = timeout_seconds

    def insert(self, events: list[CanonicalOperationalEvent]) -> None:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            grouped[event.event_kind].append(event.row)
        for event_kind, rows in grouped.items():
            table = EVENT_TABLES.get(event_kind)
            if table is None:
                raise ValueError(f"unsupported operational event kind: {event_kind}")
            if not isinstance(table, str):
                raise ValueError(
                    f"operational event kind must be expanded before insert: {event_kind}"
                )
            payload = "\n".join(
                json.dumps(row, separators=(",", ":"), sort_keys=True) for row in rows
            ).encode("utf-8")
            command = ["/usr/bin/clickhouse-client"]
            # Omitted, not defaulted: see the class docstring. With no host,
            # port or TLS configured this list is exactly what it has always
            # been, so the single-node deployment is untouched.
            if self._host:
                command += ["--host", self._host]
            if self._port:
                command += ["--port", str(self._port)]
            if self._secure:
                command.append("--secure")
            command += [
                "--user",
                self._user,
                "--database",
                self._database,
                "--query",
                f"INSERT INTO {table} FORMAT JSONEachRow",
            ]
            env = os.environ.copy()
            env["CLICKHOUSE_PASSWORD"] = self._password
            try:
                result = subprocess.run(  # noqa: S603 - fixed executable and table allowlist.
                    command,
                    input=payload,
                    env=env,
                    # Do not inherit the daemon's cwd.  The systemd unit must
                    # keep /opt/tr-clickhouse as WorkingDirectory so `python
                    # -m` can import the installed package, but an installer
                    # rotating that tree must not make every future child die
                    # before clickhouse-client can process its arguments.
                    cwd="/",
                    capture_output=True,
                    check=False,
                    timeout=self._timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                # Raising is the point: the caller must not reach its DELETE,
                # so the rows stay queued and the next sweep retries them.
                raise ClickHouseInsertError(
                    f"ClickHouse {event_kind} insert to "
                    f"{self._host or 'localhost'} timed out after "
                    f"{self._timeout_seconds}s"
                ) from exc
            except OSError as exc:
                # Includes exec failures such as a missing binary.  Keep these
                # distinguishable from source, normalisation and DELETE errors
                # so the Postgres daemon's liveness bound only counts a writer
                # that is actually unable to deliver.
                raise ClickHouseInsertError(
                    f"ClickHouse {event_kind} insert could not start: {exc}"
                ) from exc
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", errors="replace")[:1000]
                raise ClickHouseInsertError(
                    f"ClickHouse {event_kind} insert failed: {detail}"
                )


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
    elif row.event_kind == "spend_lease_shadow":
        allowed = SPEND_LEASE_SHADOW_COLUMNS
        required = tuple(
            column for column in SPEND_LEASE_SHADOW_COLUMNS if column != "no_lease_reason"
        )
        if raw.get("schema_version") != 1:
            raise ValueError("spend_lease_shadow schema_version must be 1")
        if raw.get("event_id") != row.event_id:
            raise ValueError("spend_lease_shadow event_id does not match its outbox key")
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
        if row.event_kind == "spend_lease_shadow" and column in {
            "boot_verified",
            "would_admit",
        }:
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


def drain_once(
    source: OutboxSource,
    writer: BatchWriter,
    *,
    batch_size: int,
) -> DrainResult:
    rows = source.fetch(limit=batch_size)
    if not rows:
        return DrainResult(fetched=0, inserted=0, rows_per_second=0.0)
    lag_seconds = _lag_seconds(min(row.commit_ts for row in rows))
    events: list[CanonicalOperationalEvent] = []
    quarantined = 0
    for row in rows:
        try:
            events.extend(normalise_operational_event(row))
        except ValueError as exc:
            events.append(quarantine_event(row, exc))
            quarantined += 1
    started = time.monotonic()
    writer.insert(events)
    source.delete(rows)
    elapsed = max(time.monotonic() - started, 0.000_001)
    return DrainResult(
        fetched=len(rows),
        inserted=len(events) - quarantined,
        rows_per_second=len(events) / elapsed,
        quarantined=quarantined,
        lag_seconds=lag_seconds,
    )


def _lag_seconds(oldest: dt.datetime | None) -> float:
    if oldest is None:
        return 0.0
    return max(0.0, (dt.datetime.now(dt.UTC) - _utc(oldest)).total_seconds())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", PROJECT))
    parser.add_argument(
        "--spanner-instance",
        default=os.environ.get("SPANNER_INSTANCE_ID", SPANNER_INSTANCE),
    )
    parser.add_argument(
        "--spanner-database",
        default=os.environ.get("SPANNER_DATABASE_ID", SPANNER_DATABASE),
    )
    parser.add_argument("--shards", type=int, default=OUTBOX_SHARDS)
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    password = os.environ.get("CH_PASSWORD", "")
    if not password:
        raise SystemExit("CH_PASSWORD is required")
    source = SpannerOperationalOutboxSource(
        project=args.project,
        instance=args.spanner_instance,
        database=args.spanner_database,
        shard_count=args.shards,
    )
    writer = ClickHouseOperationalWriter(password=password)
    # Idle backoff: the poll interval doubles while the outbox stays empty
    # (cap 30s) and snaps back to fast the moment work appears. Combined with
    # the single-statement fetch this takes the idle cost from 64 statements
    # per ~1.5s to one statement per 30s. Lag is measured from the batch in
    # hand: an empty outbox HAS no lag, and the per-cycle 32-statement
    # watermark this replaces was the single largest query load on the
    # production Spanner instance (measured 2026-08-25).
    idle_delay = max(0.1, args.poll_seconds)
    sd_notify("READY=1")
    while True:
        sd_notify("WATCHDOG=1")
        result = drain_once(source, writer, batch_size=max(1, args.batch_size))
        if result.fetched:
            log.info(
                "operational_analytics_outbox.metrics rows=%d rows_per_second=%.3f "
                "drain_lag_seconds=%.3f quarantined=%d",
                result.inserted,
                result.rows_per_second,
                result.lag_seconds,
                result.quarantined,
            )
        if args.once:
            return 0
        if result.fetched == 0:
            time.sleep(idle_delay)
            idle_delay = min(idle_delay * 2, 30.0)
        else:
            idle_delay = max(0.1, args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
