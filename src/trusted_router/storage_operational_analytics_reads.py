"""Shared ClickHouse record readers for the durable stores."""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

from trusted_router.operational_analytics import OperationalAnalyticsClient
from trusted_router.storage_activity import (
    ActivityResult,
    filter_generations,
    generation_events,
    generation_metrics,
    summarize_activity_result,
    usage_bucket_key,
)
from trusted_router.storage_models import Generation
from trusted_router.storage_operational_analytics import analytics_surrogate


def create_operational_analytics_client(
    *,
    url: str,
    user: str,
    password: str,
    database: str,
) -> OperationalAnalyticsClient | None:
    return (
        OperationalAnalyticsClient(base_url=url, user=user, password=password, database=database)
        if url and password
        else None
    )


def _empty_usage_bucket(bucket: str) -> dict[str, Any]:
    return {
        "bucket": bucket,
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cost_micro": 0,
        "byok_micro": 0,
    }


def _add_usage_metrics(bucket: dict[str, Any], metrics: dict[str, int]) -> None:
    for key, value in metrics.items():
        bucket[key] += value


class OperationalAnalyticsReads:
    _operational_analytics: OperationalAnalyticsClient | None

    def activity(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = None,
        date: str | None = None,
        tag_key: str | None = None,
        tag_value: str | None = None,
        group_by_tag: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.activity_result(
            workspace_id,
            api_key_hash=api_key_hash,
            date=date,
            tag_key=tag_key,
            tag_value=tag_value,
            group_by_tag=group_by_tag,
        ).data

    def activity_events(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = None,
        date: str | None = None,
        limit: int = 100,
        tag_key: str | None = None,
        tag_value: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.activity_events_result(
            workspace_id,
            api_key_hash=api_key_hash,
            date=date,
            limit=limit,
            tag_key=tag_key,
            tag_value=tag_value,
        ).data

    def activity_result(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = None,
        date: str | None = None,
        tag_key: str | None = None,
        tag_value: str | None = None,
        group_by_tag: str | None = None,
    ) -> Any:
        return self._clickhouse_activity_result(
            workspace_id,
            api_key_hash=api_key_hash,
            date=date,
            tag_key=tag_key,
            tag_value=tag_value,
            group_by_tag=group_by_tag,
        )

    def activity_events_result(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = None,
        date: str | None = None,
        limit: int = 100,
        tag_key: str | None = None,
        tag_value: str | None = None,
    ) -> Any:
        return self._clickhouse_activity_events_result(
            workspace_id,
            api_key_hash=api_key_hash,
            date=date,
            limit=limit,
            tag_key=tag_key,
            tag_value=tag_value,
        )

    def usage_series(
        self,
        workspace_id: str,
        *,
        window_minutes: int,
        granularity: str,
        api_key_hash: str | None = None,
        by_model: bool = False,
    ) -> dict[str, Any]:
        if granularity == "day":
            days = max(1, window_minutes // 1440)
            today = dt.datetime.now(dt.UTC).date()
            start_day = (today - dt.timedelta(days=days - 1)).isoformat()
            end_day = today.isoformat()
            min_created_at = None
        else:
            now = dt.datetime.now(dt.UTC)
            since = now - dt.timedelta(minutes=max(1, window_minutes))
            start_day = since.date().isoformat()
            end_day = now.date().isoformat()
            min_created_at = since.strftime("%Y-%m-%dT%H:%M:%S")
        return self._clickhouse_usage_series(
            workspace_id,
            start_day=start_day,
            end_day=end_day,
            granularity=granularity,
            api_key_hash=api_key_hash,
            by_model=by_model,
            min_created_at=min_created_at,
        )

    def _require_operational_analytics(self) -> OperationalAnalyticsClient:
        if self._operational_analytics is None:
            raise RuntimeError("operational ClickHouse reader is not configured")
        return self._operational_analytics

    def _clickhouse_activity_rows(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None,
        date: str | None,
        limit: int,
        tag_key: str | None = None,
        tag_value: str | None = None,
    ) -> list[Generation]:
        tenant_id = analytics_surrogate("workspace", workspace_id)
        key_id = analytics_surrogate("api-key", api_key_hash) if api_key_hash is not None else None
        rows = self._require_operational_analytics().activity_generations(
            tenant_id=tenant_id,
            key_id=key_id,
            tag_key=tag_key,
            tag_value=tag_value,
            date=date,
            limit=limit,
        )
        return filter_generations(
            rows,
            workspace_id=tenant_id,
            date=date,
        )

    def _clickhouse_activity_result(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None,
        date: str | None,
        tag_key: str | None,
        tag_value: str | None,
        group_by_tag: str | None,
    ) -> ActivityResult:
        scan_limit = 5000
        rows = self._clickhouse_activity_rows(
            workspace_id,
            api_key_hash=api_key_hash,
            date=date,
            limit=scan_limit + 1,
            tag_key=tag_key,
            tag_value=tag_value,
        )
        truncated = len(rows) > scan_limit
        rows = rows[:scan_limit]
        scanned = len(rows)
        rows = filter_generations(
            rows,
            workspace_id=analytics_surrogate("workspace", workspace_id),
        )
        result = summarize_activity_result(
            rows,
            group_by_tag=group_by_tag,
            truncated=truncated,
            scan_limit=scan_limit,
        )
        return dataclasses.replace(result, scanned=scanned)

    def _clickhouse_activity_events_result(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None,
        date: str | None,
        limit: int,
        tag_key: str | None,
        tag_value: str | None,
    ) -> ActivityResult:
        normalized_limit = max(1, min(limit, 1000))
        rows = self._clickhouse_activity_rows(
            workspace_id,
            api_key_hash=api_key_hash,
            date=date,
            limit=normalized_limit + 1,
            tag_key=tag_key,
            tag_value=tag_value,
        )
        truncated = len(rows) > normalized_limit
        rows = rows[:normalized_limit]
        scanned = len(rows)
        rows = filter_generations(
            rows,
            workspace_id=analytics_surrogate("workspace", workspace_id),
        )
        return ActivityResult(
            data=generation_events(rows, limit=normalized_limit),
            truncated=truncated,
            scanned=scanned,
            scan_limit=normalized_limit,
        )

    def _clickhouse_usage_series(
        self,
        workspace_id: str,
        *,
        start_day: str,
        end_day: str,
        granularity: str,
        api_key_hash: str | None,
        by_model: bool,
        min_created_at: str | None,
    ) -> dict[str, Any]:
        tenant_id = analytics_surrogate("workspace", workspace_id)
        key_id = analytics_surrogate("api-key", api_key_hash) if api_key_hash is not None else None
        end_exclusive = (dt.date.fromisoformat(end_day) + dt.timedelta(days=1)).isoformat()
        rows = self._require_operational_analytics().activity_generations(
            tenant_id=tenant_id,
            key_id=key_id,
            start_at=min_created_at or start_day,
            end_at=end_exclusive,
            limit=200_001,
        )
        truncated = len(rows) > 200_000
        rows = rows[:200_000]
        buckets: dict[str, dict[str, Any]] = {}
        by_model_buckets: dict[str, dict[str, dict[str, Any]]] = {}
        for generation in rows:
            bucket_id = usage_bucket_key(generation.created_at, granularity)
            bucket = buckets.setdefault(bucket_id, _empty_usage_bucket(bucket_id))
            _add_usage_metrics(bucket, generation_metrics(generation))
            if by_model:
                model_bucket = by_model_buckets.setdefault(
                    generation.model,
                    {},
                ).setdefault(bucket_id, _empty_usage_bucket(bucket_id))
                _add_usage_metrics(model_bucket, generation_metrics(generation))
        result: dict[str, Any] = {
            "granularity": granularity,
            "start_day": start_day,
            "end_day": end_day,
            "truncated": truncated,
            "buckets": [buckets[key] for key in sorted(buckets)],
        }
        if by_model:
            result["by_model"] = {
                model: [model_buckets[key] for key in sorted(model_buckets)]
                for model, model_buckets in sorted(by_model_buckets.items())
            }
        return result
