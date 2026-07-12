from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from trusted_router.money import microdollars_to_float
from trusted_router.storage_models import Generation, _is_byok


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
        if gen.workspace_id == workspace_id
        and (api_key_hash is None or gen.key_hash == api_key_hash)
        and (date is None or gen.created_at.startswith(date))
        and (tag_key is None or tag_key in gen.tags)
        and (tag_value is None or gen.tags.get(tag_key or "") == tag_value)
    ]


def summarize_activity(
    generations: Iterable[Generation], *, group_by_tag: str | None = None
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
    for gen in generations:
        day = gen.created_at[:10]
        grouped_tag_value = gen.tags.get(group_by_tag) if group_by_tag else None
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
    return sorted(grouped.values(), key=lambda item: item["date"], reverse=True)


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
