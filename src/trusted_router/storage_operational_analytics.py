"""Backend-neutral shape of the operational-analytics outbox.

The tenant-activity and synthetic-status stream has the same privacy contract
on every cloud: raw workspace ids and API key hashes never leave the system of
record, ClickHouse receives stable one-way surrogates and content-free request
metadata only.  That contract lives in the payload projection, so the
projection must be shared rather than reimplemented per backend — a second
copy is a second place for a raw identifier to leak in.

Only the *durability mechanism* differs per backend (Spanner commit
timestamps vs a Postgres primary key), so only the writer classes are
backend-specific.  Everything here is pure: no cloud SDKs, no IO.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import TYPE_CHECKING, Any, Protocol

from trusted_router.catalog import MODELS
from trusted_router.client_reliability import (
    METHODOLOGY_VERSION,
    classify_tr_fault,
    timeout_floor_met,
)
from trusted_router.storage_models import Generation, SyntheticProbeSample

if TYPE_CHECKING:
    from trusted_router.client_events_schema import ClientEventsBatch

OPERATIONAL_ANALYTICS_OUTBOX_SHARDS = 32


class OperationalAnalyticsWriter(Protocol):
    """What the stores need from an operational-telemetry writer.

    Two implementations: the durable outboxes (Spanner/Postgres rows drained
    by a poller) and DirectOperationalAnalyticsSink (straight to ClickHouse,
    no billing-database involvement). The stores hold this Protocol so the
    choice is wiring, not code.
    """

    def enqueue_activity(self, generation: Any) -> None: ...
    def enqueue_activity_tx(self, transaction: Any, generation: Any) -> None: ...
    def enqueue_synthetic(self, sample: Any) -> None: ...
    def enqueue_synthetic_tx(self, transaction: Any, sample: Any) -> None: ...
    def enqueue_client_events(self, payload: dict[str, Any]) -> None: ...
    def oldest_enqueued_at(self, *, timeout: float | None = None) -> dt.datetime | None: ...


ACTIVITY_EVENT_KIND = "activity"
SYNTHETIC_EVENT_KIND = "synthetic"
CLIENT_EVENTS_EVENT_KIND = "client_events"


def operational_analytics_shard(
    event_id: str,
    *,
    shard_count: int = OPERATIONAL_ANALYTICS_OUTBOX_SHARDS,
) -> int:
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    digest = hashlib.blake2b(event_id.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % shard_count


def analytics_surrogate(namespace: str, value: str) -> str:
    """Return a stable non-reversible identifier for private analytics."""
    material = f"trustedrouter:{namespace}:{value}".encode()
    return hashlib.sha256(material).hexdigest()


def _iso_milliseconds(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_client_events_payload(
    batch: ClientEventsBatch,
    *,
    tenant_id: str,
    key_id: str,
    received_at: dt.datetime,
    is_synthetic: bool,
    success_sample_rate: float,
) -> dict[str, Any]:
    """Project one validated beacon batch onto the ClickHouse outbox shape."""
    _ = success_sample_rate
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=dt.UTC)
    received_at = received_at.astimezone(dt.UTC)
    received_at_text = _iso_milliseconds(received_at)
    clock_skew_ms = 0
    if batch.sent_at_ms is not None:
        clock_skew_ms = int(received_at.timestamp() * 1000) - batch.sent_at_ms
        clock_skew_ms = max(-86_400_000, min(clock_skew_ms, 86_400_000))

    events: list[dict[str, Any]] = []
    for event in batch.events:
        attempts = event.attempts
        outcome = (
            attempts[-1].outcome if event.final_outcome == "exhausted" else event.final_outcome
        )
        error_class = next(
            (attempt.error_class for attempt in attempts if attempt.error_class is not None),
            None,
        )
        error_source = next(
            (attempt.error_source for attempt in attempts if attempt.error_source is not None),
            None,
        )
        event_payload = event.model_dump(mode="json")
        event_payload.pop("age_ms")
        if event.model is not None:
            event_payload["model"] = event.model if event.model in MODELS else "other"
        event_payload.update(
            {
                "created_at": _iso_milliseconds(
                    received_at - dt.timedelta(milliseconds=event.age_ms)
                ),
                "tr_fault": int(
                    classify_tr_fault(
                        level="request",
                        outcome=outcome,
                        error_class=error_class,
                        error_source=error_source,
                        http_status_class_or_status=event.final_http_status,
                        host=attempts[-1].host,
                        provider_pinned=event.provider_pinned,
                        timeout_phase=event.timeout_phase,
                        timeout_floor_met=timeout_floor_met(
                            event.timeout_phase,
                            event.configured_timeout_ms,
                        ),
                    )
                ),
                "methodology_version": METHODOLOGY_VERSION,
            }
        )
        events.append(event_payload)

    counters: list[dict[str, Any]] = []
    for counter in batch.counters:
        bucket_start = received_at - dt.timedelta(milliseconds=counter.window_start_age_ms)
        counter_payload = counter.model_dump(mode="json")
        counter_payload.pop("window_start_age_ms")
        counter_payload.update(
            {
                "bucket_start": _iso_milliseconds(bucket_start.replace(second=0, microsecond=0)),
                "tr_fault": int(
                    classify_tr_fault(
                        level=counter.level,
                        outcome=counter.outcome,
                        error_class=counter.error_class,
                        error_source=None,
                        http_status_class_or_status=counter.http_status_class,
                        host=counter.host,
                        provider_pinned=counter.provider_pinned,
                        timeout_phase=counter.timeout_phase,
                        timeout_floor_met=counter.timeout_floor_met,
                    )
                ),
                "methodology_version": METHODOLOGY_VERSION,
            }
        )
        counters.append(counter_payload)

    return {
        "schema_version": batch.schema_version,
        "tenant_id": analytics_surrogate("workspace", tenant_id),
        "key_id": analytics_surrogate("api-key", key_id),
        "received_at": received_at_text,
        "clock_skew_ms": clock_skew_ms,
        "synthetic": bool(batch.synthetic or is_synthetic),
        "batch_id": batch.batch_id,
        "instance_id": batch.instance_id,
        "seq": batch.seq,
        "sdk": batch.sdk.model_dump(mode="json"),
        "events": events,
        "counters": counters,
    }


def activity_payload(generation: Generation) -> dict[str, Any]:
    """Project a generation onto the content-free tenant activity schema."""
    return {
        "generation_id": generation.id,
        "request_id": generation.request_id,
        "tenant_id": analytics_surrogate("workspace", generation.workspace_id),
        # The raw id, alongside the surrogate. The surrogate stays because it
        # is the ClickHouse sort key; the raw id is deliberate (2026-08-19):
        # workspace ids are pseudonymous, ClickHouse holds no emails, and rows
        # that name their workspace need no directory refresh to be joinable.
        "workspace_id": generation.workspace_id,
        "key_id": analytics_surrogate("api-key", generation.key_hash),
        "model": generation.model,
        "provider": generation.provider or "",
        "provider_name": generation.provider_name,
        "app": generation.app,
        "tokens_prompt": generation.tokens_prompt,
        "tokens_completion": generation.tokens_completion,
        "cached_input_tokens": generation.cached_input_tokens,
        "reasoning_tokens": generation.reasoning_tokens,
        "total_cost_microdollars": generation.total_cost_microdollars,
        "usage_type": str(generation.usage_type),
        "speed_tokens_per_second": generation.speed_tokens_per_second,
        "finish_reason": generation.finish_reason,
        "status": generation.status,
        "streamed": generation.streamed,
        "usage_estimated": generation.usage_estimated,
        "elapsed_milliseconds": generation.elapsed_milliseconds,
        "first_token_milliseconds": generation.first_token_milliseconds,
        "ttfb_milliseconds": generation.ttfb_milliseconds,
        "region": generation.region,
        "user": generation.user,
        "session_id": generation.session_id,
        "http_referer": generation.http_referer,
        "app_categories": list(generation.app_categories),
        "tags": dict(generation.tags),
        # R-PR2 declares these as node-side columns. Until then the ingester
        # projects only its declared columns and safely ignores these extras.
        "gateway_request_id": generation.gateway_request_id,
        "synthetic": generation.synthetic,
        "client_source": generation.client_source,
        "client_sdk": generation.client_sdk,
        "client_sdk_version": generation.client_sdk_version,
        "client_lang": generation.client_lang,
        "client_runtime": generation.client_runtime,
        "client_os": generation.client_os,
        "client_arch": generation.client_arch,
        "client_timeout_ms": generation.client_timeout_ms,
        "client_attempt": generation.client_attempt,
        "client_prev_outcome": generation.client_prev_outcome,
        "client_prev_error_class": generation.client_prev_error_class,
        "client_prev_host": generation.client_prev_host,
        "client_prev_elapsed_ms": generation.client_prev_elapsed_ms,
        "client_since_first_ms": generation.client_since_first_ms,
        "client_stream": generation.client_stream,
        "client_failover_used": generation.client_failover_used,
        "created_at": generation.created_at,
    }


def synthetic_payload(sample: SyntheticProbeSample) -> dict[str, Any]:
    return sample.public_dict()
