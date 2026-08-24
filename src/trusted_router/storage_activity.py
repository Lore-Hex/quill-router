from __future__ import annotations

import copy
import dataclasses
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from trusted_router.money import microdollars_to_float
from trusted_router.storage_models import Generation, _is_byok

MAX_TAG_GROUP_VALUES = 100
ACTIVITY_TAG_CACHE_TTL_SECONDS = 15.0
ACTIVITY_TAG_CACHE_MAX_ENTRIES = 512

ActivityTagCacheKey = tuple[
    str,
    str | None,
    str | None,
    str | None,
    int | None,
    str,
    str | None,
]


@dataclass(frozen=True)
class ActivityResult:
    data: list[dict[str, Any]]
    truncated: bool = False
    groups_truncated: bool = False
    scanned: int = 0
    scan_limit: int | None = None


@dataclass(frozen=True)
class _ActivityTagCacheEntry:
    result: ActivityResult
    expires_at: float


class ActivityTagCache:
    """Small per-process cache for repeated tag-filtered dashboard polls."""

    def __init__(
        self,
        *,
        ttl_seconds: float = ACTIVITY_TAG_CACHE_TTL_SECONDS,
        max_entries: int = ACTIVITY_TAG_CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("activity tag cache TTL must be positive")
        if max_entries < 1:
            raise ValueError("activity tag cache max_entries must be positive")
        self._ttl_seconds = float(ttl_seconds)
        self._max_entries = int(max_entries)
        self._clock = clock
        self._entries: OrderedDict[ActivityTagCacheKey, _ActivityTagCacheEntry] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: ActivityTagCacheKey) -> ActivityResult | None:
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            result = entry.result
        # Hand every hit its own copy of the payload: callers mutate returned
        # event dicts in place (e.g. console cost_display annotation), and a
        # shared cached list would leak one caller's mutations into the next.
        return dataclasses.replace(result, data=copy.deepcopy(result.data))

    def put(self, key: ActivityTagCacheKey, result: ActivityResult) -> None:
        entry = _ActivityTagCacheEntry(
            result=result,
            expires_at=self._clock() + self._ttl_seconds,
        )
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# 15s staleness is acceptable for analytics polling and avoids repeated
# tag-filtered Bigtable scans. Mutating writes do not invalidate this cache.
ACTIVITY_TAG_CACHE = ActivityTagCache()


def generation_metrics(gen: Generation) -> dict[str, int]:
    is_byok = _is_byok(gen.usage_type)
    return {
        "requests": 1,
        "prompt_tokens": gen.tokens_prompt,
        "completion_tokens": gen.tokens_completion,
        "reasoning_tokens": gen.reasoning_tokens,
        "cost_micro": 0 if is_byok else gen.total_cost_microdollars,
        "byok_micro": gen.total_cost_microdollars if is_byok else 0,
    }


def usage_bucket_key(created_at: str, granularity: str) -> str:
    if granularity == "minute":
        return created_at[:16]
    if granularity == "5min":
        return created_at[:14] + f"{(int(created_at[14:16]) // 5) * 5:02d}"
    if granularity == "hour":
        return created_at[:13]
    if granularity == "day":
        return created_at[:10]
    raise ValueError(f"unknown granularity: {granularity}")


def filter_generations(
    generations: Iterable[Generation],
    *,
    workspace_id: str,
    api_key_hash: str | None = None,
    date: str | None = None,
    tag_key: str | None = None,
    tag_value: str | None = None,
) -> list[Generation]:
    return [
        gen
        for gen in generations
        if generation_matches_filter(
            gen,
            workspace_id=workspace_id,
            api_key_hash=api_key_hash,
            date=date,
            tag_key=tag_key,
            tag_value=tag_value,
        )
    ]


def generation_matches_filter(
    gen: Generation,
    *,
    workspace_id: str,
    api_key_hash: str | None = None,
    date: str | None = None,
    tag_key: str | None = None,
    tag_value: str | None = None,
) -> bool:
    return (
        gen.workspace_id == workspace_id
        and (api_key_hash is None or gen.key_hash == api_key_hash)
        and (date is None or gen.created_at.startswith(date))
        and (tag_key is None or tag_key in gen.tags)
        and (tag_value is None or gen.tags.get(tag_key or "") == tag_value)
    )


def summarize_activity(
    generations: Iterable[Generation], *, group_by_tag: str | None = None
) -> list[dict[str, Any]]:
    return summarize_activity_result(generations, group_by_tag=group_by_tag).data


def summarize_activity_result(
    generations: Iterable[Generation],
    *,
    group_by_tag: str | None = None,
    truncated: bool = False,
    scan_limit: int | None = None,
) -> ActivityResult:
    generation_rows = list(generations)
    retained_values: set[str | None] | None = None
    groups_truncated = False
    if group_by_tag:
        counts = Counter(gen.tags.get(group_by_tag) for gen in generation_rows)
        if len(counts) > MAX_TAG_GROUP_VALUES:
            ranked = sorted(counts, key=lambda value: (-counts[value], str(value)))
            retained_values = set(ranked[:MAX_TAG_GROUP_VALUES])
            groups_truncated = True

    grouped: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
    for gen in generation_rows:
        day = gen.created_at[:10]
        grouped_tag_value = gen.tags.get(group_by_tag) if group_by_tag else None
        if retained_values is not None and grouped_tag_value not in retained_values:
            grouped_tag_value = "__other__"
        key = (day, gen.model, gen.provider_name, grouped_tag_value)
        item = grouped.setdefault(
            key,
            {
                "date": day,
                "model": gen.model,
                "provider_name": gen.provider_name,
                "endpoint_id": gen.provider_name.lower().replace(" ", "-"),
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "usage": 0.0,
                "usage_microdollars": 0,
                "byok_usage_inference": 0.0,
                "byok_usage_inference_microdollars": 0,
                **(
                    {"tag_key": group_by_tag, "tag_value": grouped_tag_value}
                    if group_by_tag
                    else {}
                ),
            },
        )
        metrics = generation_metrics(gen)
        item["requests"] += metrics["requests"]
        item["prompt_tokens"] += metrics["prompt_tokens"]
        item["completion_tokens"] += metrics["completion_tokens"]
        item["reasoning_tokens"] += metrics["reasoning_tokens"]
        item["usage_microdollars"] += metrics["cost_micro"]
        item["byok_usage_inference_microdollars"] += metrics["byok_micro"]
        item["usage"] += microdollars_to_float(metrics["cost_micro"])
        item["byok_usage_inference"] += microdollars_to_float(metrics["byok_micro"])
    return ActivityResult(
        data=sorted(grouped.values(), key=lambda item: item["date"], reverse=True),
        truncated=truncated,
        groups_truncated=groups_truncated,
        scanned=len(generation_rows),
        scan_limit=scan_limit,
    )


def generation_events(
    generations: Iterable[Generation],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    rows = sorted(generations, key=lambda gen: gen.created_at, reverse=True)
    if limit is not None:
        rows = rows[:limit]
    return [
        {
            "id": gen.id,
            "request_id": gen.request_id,
            "created_at": gen.created_at,
            "date": gen.created_at[:10],
            "model": gen.model,
            "provider_name": gen.provider_name,
            "app": gen.app,
            "user": gen.user,
            "session_id": gen.session_id,
            "http_referer": gen.http_referer,
            "app_categories": list(gen.app_categories),
            "tags": dict(gen.tags),
            "input_tokens": gen.tokens_prompt,
            "output_tokens": gen.tokens_completion,
            "cost": microdollars_to_float(gen.total_cost_microdollars),
            "cost_microdollars": gen.total_cost_microdollars,
            "usage_type": gen.usage_type,
            "speed_tokens_per_second": gen.speed_tokens_per_second,
            "finish_reason": gen.finish_reason,
            "status": gen.status,
            "streamed": gen.streamed,
            "content_stored": False,
        }
        for gen in rows
    ]
