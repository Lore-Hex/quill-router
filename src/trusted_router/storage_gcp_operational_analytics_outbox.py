"""Durable outbox for tenant activity and synthetic status metadata.

This outbox is separate from the public/provider benchmark stream because its
tables have different privacy and retention boundaries.  Raw workspace and API
key identifiers never leave Spanner: ClickHouse receives stable one-way
surrogates and content-free request metadata only.
"""

from __future__ import annotations

import hashlib
from typing import Any

from trusted_router.storage_gcp_codec import json_body
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
        "created_at": generation.created_at,
    }


def synthetic_payload(sample: SyntheticProbeSample) -> dict[str, Any]:
    return sample.public_dict()


class SpannerOperationalAnalyticsOutbox:
    """Append immutable operational events using Spanner commit timestamps."""

    def __init__(
        self,
        database: Any,
        param_types: Any,
        *,
        shard_count: int = OPERATIONAL_ANALYTICS_OUTBOX_SHARDS,
    ) -> None:
        if shard_count < 1:
            raise ValueError("shard_count must be positive")
        self._database = database
        self._pt = param_types
        self._shard_count = shard_count

    def enqueue_activity(self, generation: Generation) -> None:
        self._enqueue(
            event_kind=ACTIVITY_EVENT_KIND,
            event_id=generation.id,
            payload=activity_payload(generation),
        )

    def enqueue_synthetic(self, sample: SyntheticProbeSample) -> None:
        self._enqueue(
            event_kind=SYNTHETIC_EVENT_KIND,
            event_id=sample.id,
            payload=synthetic_payload(sample),
        )

    def _enqueue(
        self,
        *,
        event_kind: str,
        event_id: str,
        payload: dict[str, Any],
    ) -> None:
        shard = operational_analytics_shard(
            f"{event_kind}:{event_id}",
            shard_count=self._shard_count,
        )

        def txn(transaction: Any) -> None:
            transaction.execute_update(
                "INSERT INTO tr_operational_analytics_outbox "
                "(shard, commit_ts, event_kind, event_id, payload) "
                "VALUES (@shard, PENDING_COMMIT_TIMESTAMP(), @event_kind, "
                "@event_id, @payload)",
                params={
                    "shard": shard,
                    "event_kind": event_kind,
                    "event_id": event_id,
                    "payload": json_body(payload),
                },
                param_types={
                    "shard": self._pt.INT64,
                    "event_kind": self._pt.STRING,
                    "event_id": self._pt.STRING,
                    "payload": self._pt.STRING,
                },
            )

        self._database.run_in_transaction(txn)
