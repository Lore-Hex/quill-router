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

import hashlib
from typing import Any

from trusted_router.storage_models import Generation, SyntheticProbeSample

OPERATIONAL_ANALYTICS_OUTBOX_SHARDS = 32
ACTIVITY_EVENT_KIND = "activity"
SYNTHETIC_EVENT_KIND = "synthetic"


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


def activity_payload(generation: Generation) -> dict[str, Any]:
    """Project a generation onto the content-free tenant activity schema."""
    return {
        "generation_id": generation.id,
        "request_id": generation.request_id,
        "tenant_id": analytics_surrogate("workspace", generation.workspace_id),
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
