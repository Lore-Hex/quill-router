"""Shared ClickHouse record readers for the durable stores."""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

from trusted_router.operational_analytics import (
    OperationalAnalyticsClient,
    bound_values_stay_private,
)
from trusted_router.storage_activity import (
    ActivityResult,
    filter_generations,
    generation_events,
    summarize_activity_result,
)
from trusted_router.storage_activity_tags import TAG_EVENT_LIMIT, tag_summary
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


_USAGE_METRICS = (
    "requests",
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cost_micro",
    "byok_micro",
)
# A newest-first read with no date filter looks at a recent window first and
# widens only while the window holds fewer rows than were asked for. A window
# that fills the limit already holds the newest rows of the whole history, so
# the result is the same as the unbounded read; a busy workspace stops at the
# first window instead of reading everything it ever sent.
_RECENT_ACTIVITY_WINDOWS = (dt.timedelta(days=1), dt.timedelta(days=30))


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

    def activity_tag_summary(
        self,
        workspace_id: str,
        *,
        start_at: str,
        group_by: str | None = None,
        tag_key: str | None = None,
        tag_value: str | None = None,
    ) -> dict[str, Any]:
        client = self._require_operational_analytics()
        tenant_id = analytics_surrogate("workspace", workspace_id)
        return tag_summary(
            read_keys=lambda limit: client.activity_tag_keys(
                tenant_id=tenant_id, start_at=start_at, limit=limit
            ),
            read_values=lambda key, limit: client.activity_tag_values(
                tenant_id=tenant_id, tag_key=key, start_at=start_at, limit=limit
            ),
            read_breakdown=lambda key, limit: client.activity_tag_breakdown(
                tenant_id=tenant_id,
                group_by=key,
                start_at=start_at,
                limit=limit,
                tag_key=tag_key,
                tag_value=tag_value,
            ),
            group_by=group_by,
            tag_key=tag_key,
        )

    def activity_tag_events(
        self,
        workspace_id: str,
        *,
        start_at: str,
        tag_key: str,
        tag_value: str | None = None,
        limit: int = TAG_EVENT_LIMIT,
    ) -> Any:
        normalized_limit = max(1, min(limit, TAG_EVENT_LIMIT))
        tenant_id = analytics_surrogate("workspace", workspace_id)
        with bound_values_stay_private():
            rows = self._require_operational_analytics().activity_generations(
                tenant_id=tenant_id,
                tag_key=tag_key,
                tag_value=tag_value,
                start_at=start_at,
                limit=normalized_limit + 1,
            )
        truncated = len(rows) > normalized_limit
        rows = filter_generations(rows[:normalized_limit], workspace_id=tenant_id)
        return ActivityResult(
            data=generation_events(rows, limit=normalized_limit),
            truncated=truncated,
            scanned=len(rows),
            scan_limit=normalized_limit,
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
        start_at: str | None = None,
    ) -> list[Generation]:
        tenant_id = analytics_surrogate("workspace", workspace_id)
        key_id = analytics_surrogate("api-key", api_key_hash) if api_key_hash is not None else None
        rows = self._require_operational_analytics().activity_generations(
            tenant_id=tenant_id,
            key_id=key_id,
            tag_key=tag_key,
            tag_value=tag_value,
            date=date,
            start_at=start_at,
            limit=limit,
        )
        return filter_generations(
            rows,
            workspace_id=tenant_id,
            date=date,
        )

    def _clickhouse_newest_activity_rows(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None,
        limit: int,
        tag_key: str | None,
        tag_value: str | None,
    ) -> list[Generation]:
        """The newest ``limit`` rows of the whole history, read from recent windows first."""
        now = dt.datetime.now(dt.UTC)
        for window in _RECENT_ACTIVITY_WINDOWS:
            rows = self._clickhouse_activity_rows(
                workspace_id,
                api_key_hash=api_key_hash,
                date=None,
                limit=limit,
                tag_key=tag_key,
                tag_value=tag_value,
                start_at=(now - window).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            if len(rows) >= limit:
                return rows
        return self._clickhouse_activity_rows(
            workspace_id,
            api_key_hash=api_key_hash,
            date=None,
            limit=limit,
            tag_key=tag_key,
            tag_value=tag_value,
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
        if date is None:
            rows = self._clickhouse_newest_activity_rows(
                workspace_id,
                api_key_hash=api_key_hash,
                limit=normalized_limit + 1,
                tag_key=tag_key,
                tag_value=tag_value,
            )
        else:
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
        # ClickHouse groups the range; only the grouped rows cross the wire.
        # Reading the generations and bucketing them here capped the range at
        # 200,000 rows and dropped the older part of a busy workspace's range.
        rows = self._require_operational_analytics().activity_usage_buckets(
            tenant_id=tenant_id,
            key_id=key_id,
            granularity=granularity,
            start_at=min_created_at or start_day,
            end_at=end_exclusive,
            by_model=by_model,
        )
        buckets: dict[str, dict[str, Any]] = {}
        by_model_buckets: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            bucket_id = row["bucket"]
            metrics = {key: int(row[key]) for key in _USAGE_METRICS}
            bucket = buckets.setdefault(bucket_id, _empty_usage_bucket(bucket_id))
            _add_usage_metrics(bucket, metrics)
            if by_model:
                # One query serves both views: a by-model row is one model's
                # share of its bucket, and the bucket total is their sum.
                model_bucket = by_model_buckets.setdefault(
                    row["model"],
                    {},
                ).setdefault(bucket_id, _empty_usage_bucket(bucket_id))
                _add_usage_metrics(model_bucket, metrics)
        result: dict[str, Any] = {
            "granularity": granularity,
            "start_day": start_day,
            "end_day": end_day,
            # Kept for readers of the shape. Always false: nothing is dropped.
            "truncated": False,
            "buckets": [buckets[key] for key in sorted(buckets)],
        }
        if by_model:
            result["by_model"] = {
                model: [model_buckets[key] for key in sorted(model_buckets)]
                for model, model_buckets in sorted(by_model_buckets.items())
            }
        return result
